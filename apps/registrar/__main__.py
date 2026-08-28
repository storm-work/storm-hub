"""apps/registrar entrypoint: python -m apps.registrar <run_id>

Batch account registration + email verification, ported from storm-auto's proven
app/core/batch_register.py onto storm-hub's already-vendored, DataDome-passing engine
(libs/engine/vendor: session.py's browser+primp hybrid, solver.py's CAPTCHA race, the newly-vendored
email_pool.py's mail.tm/Cloudflare inbox client) and the Hub/Bus/Supervisor architecture the rest of
this app uses. `run_id` is an ephemeral bus/process key only -- unlike a booking Worker, a
registration run has no single persisted DB row of its own; the durable per-sub-account state lives
in `registration_jobs` rows (one per sub_account_id, get-or-created by the Hub before spawn) that the
Hub's event consumer updates from the `reg_progress`/`reg_result` events this process emits. This
process never touches the DB directly -- same rule apps.worker follows.

Simplifications versus storm-auto's original (deliberate, to keep this port reviewable -- revisit if
production volume demands them): no cross-run persistent proxy/exit-IP dedup (PersistentProxyPool),
no credential auto-regeneration on exhaustion (the dashboard's "Reset" button is the operator-driven
equivalent), no identity-slot interleaving. Proxy retry is a simple round-robin over whatever pool
the Hub leased/selected at launch time.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from libs.bus import Bus, connect

from apps.worker.bus_signals import BusSignals

BASE = "https://pedidodevistos.mne.gov.pt"
REGISTER_URL = BASE + "/VistosOnline/register"
AUTH_URL = BASE + "/VistosOnline/Authentication.jsp"
VERIFY_URL = BASE + "/VistosOnline/VerificarEmail"
INSERT_TOKEN_JSP = BASE + "/VistosOnline/partials/insertToken.jsp"

MAX_PROXY_RETRIES = 5
MAX_VERIFY_RETRIES = 3
EMAIL_POLL_TIMEOUT = 240


def is_recaptcha_rejection(reg_type: str, reg_desc: str) -> bool:
    """Did the portal reject our CAPTCHA (as opposed to anything else)?

    Matters because the portal punishes repetition. Its own words, visible only once the failure
    detail stopped being truncated at 80 chars (2026-08-21):

        "o sistema apenas permite falhar o processo de ReCaptcha 2 vezes. Se tentar executar este
         processo mais vezes no periodo de tempo definido o seu acesso a plataforma de E-VISA sera
         bloqueado"

    Two failures allowed; more within the window and platform access is BLOCKED. MAX_PROXY_RETRIES
    was 5, and batches run several sub-accounts concurrently, so the retry policy could produce
    well over the threshold in a couple of minutes. "Dispoe de mais 0 tentativas" (0 attempts
    remaining) is the same counter, already exhausted.

    Deliberately matches on BOTH fields: the portal uses type=ReCaptchaError with the long warning
    in the description, and also type=ReCaptchaError with the short "0 tentativas" form.
    """
    blob = f"{reg_type} {reg_desc}".lower()
    return "recaptcha" in blob


def _mk_mail(email_provider: str, email_cfg: dict, proxy: str | None = None):
    """`proxy` is for mail.tm only: it rate-limits per IP, and every mailbox in a run was being
    created from the server's single address. 87 of ~170 registration outcomes on 2026-08-23 were
    "mail.tm create failed (429)" -- more than every portal-side cause combined. Cloudflare Email
    Routing is our own infrastructure and needs no proxy."""
    from libs.engine.vendor import CloudflareMail, MailTM
    if email_provider == "cloudflare":
        return CloudflareMail(domain=email_cfg.get("cf_mail_domain"),
                              worker_url=email_cfg.get("cf_worker_url"),
                              secret=email_cfg.get("cf_worker_secret"))
    return MailTM(proxy=proxy)


async def _register_one(acct: dict, proxies_cycle, capsolver_keys: list[str], lang: str,
                        email_provider: str, email_cfg: dict, executor: ThreadPoolExecutor,
                        bus: Bus, run_id: str, browser_sem: asyncio.Semaphore) -> str:
    """Full register+verify flow for one sub-account. Returns 'verified' | 'exists' | 'failed'.
    Every phase transition is reported via reg_progress (ephemeral, UI-only); the single settling
    outcome per phase-boundary (registered / exists / verified / failed) is reg_result, which the Hub
    persists onto that sub-account's registration_jobs row."""
    from libs.engine.vendor import HEADERS_XHR, get_session, race_all

    loop = asyncio.get_event_loop()
    sub_account_id = acct["sub_account_id"]
    username = acct["username"]

    def progress(msg: str) -> None:
        bus.emit_event(run_id, "reg_progress", sub_account_id=sub_account_id, detail=f"{username}: {msg}")

    def result(outcome: str, **extra) -> None:
        bus.emit_event(run_id, "reg_result", sub_account_id=sub_account_id, outcome=outcome, **extra)

    progress("starting")
    progress("creating email inbox")
    # Take a proxy from the same rotation the portal work uses, so mailbox creation is spread over
    # the pool rather than hammering mail.tm from one IP. peek-then-reuse: the loop below calls
    # next() again for its own attempt, and cycling one extra entry costs nothing.
    _mail_proxy = (next(proxies_cycle) or {}).get("url")
    mail = _mk_mail(email_provider, email_cfg, proxy=_mail_proxy)
    try:
        if email_provider == "cloudflare":
            email_addr, email_key = await loop.run_in_executor(executor, mail.create)
            email_pass = ""
        else:
            email_addr, email_pass = await loop.run_in_executor(executor, mail.create)
            email_key = await loop.run_in_executor(executor, mail.token, email_addr, email_pass)
        progress(f"email={email_addr} provider={email_provider}")
    except Exception as e:
        progress(f"FAIL create email: {e}")
        result("failed", detail=f"email create failed: {e}")
        return "failed"

    used_proxy = None
    used_proxy_id = None
    saved_cookies_full: list = []
    reg_token_raw = ""
    proxy_success = False
    reg_result_detail = ""

    for attempt in range(MAX_PROXY_RETRIES):
        proxy_entry = next(proxies_cycle)
        proxy = proxy_entry.get("url")
        proxy_id = proxy_entry.get("id")
        async with browser_sem:
            progress(f"loading registration page (attempt {attempt + 1}/{MAX_PROXY_RETRIES})")
            try:
                s = await loop.run_in_executor(executor, lambda p=proxy: get_session(p, headless=True))
            except Exception as e:
                progress(f"FAIL get_session ({proxy}): {e}")
                continue
            progress("register form loaded")

            progress(f"solving captcha (attempt {attempt + 1}/{MAX_PROXY_RETRIES})")
            try:
                token = await loop.run_in_executor(
                    executor, race_all, capsolver_keys, [], [], [], "REGISTER_EVISA", proxy)
            except Exception as e:
                progress(f"CAPTCHA failed: {e}")
                token = None
            if not token:
                try: s.close()
                except Exception: pass
                continue

            payload = {
                "name": acct.get("first_name", ""), "surname": acct.get("last_name", ""),
                "username": username, "password": acct["password"], "valPassword": acct["password"],
                "gender": acct.get("gender", "M"), "bday": acct.get("birthdate", "1985/01/01"),
                "nationality": acct.get("nationality", "PRT"), "traveldoc": acct.get("traveldoc", ""),
                "email": email_addr, "language": lang, "rgpd": "Y", "captchaResponse": token,
            }
            progress("form filled")
            progress("submitting registration")
            try:
                # browser_fetch, not s.post: the raw primp client shares the sticky proxy session
                # the browser above already opened, and issuing a second, separate connection
                # through that same session breaks the tunnel (measured elsewhere in this project --
                # raw-request-after-browser-use on one sticky session: 0/6 vs browser-only: 5/6).
                # 2026-08-27's reg-d0dbc700552f run showed this exact signature: CAPTCHA solving at
                # 92% (the proxy-ordering fix worked) but 89% of the *register POSTs that followed*
                # killed by "peer closed connection without sending TLS close_notify" -- the portal
                # resetting a tunnel the raw client just reused. browser_fetch submits through the
                # live browser context instead, same as every other proven request in this codebase.
                def _do(s=s, payload=payload):
                    return s.browser_fetch(REGISTER_URL, data=payload, headers=HEADERS_XHR, timeout=90)
                reg_resp = await loop.run_in_executor(executor, _do)
            except Exception as e:
                progress(f"register POST failed: {e}")
                try: s.close()
                except Exception: pass
                continue

            try:
                reg_json = json.loads(reg_resp["body"].strip())
                reg_type = reg_json.get("type", "")
                reg_desc = reg_json.get("description", "")
            except Exception:
                reg_type, reg_desc = "", reg_resp["body"][:80]

            if "already_exists" in reg_type or "already_exists" in reg_desc.lower():
                progress("account already exists on server")
                result("exists", email=email_addr, email_pass=email_pass)
                try: s.close()
                except Exception: pass
                return "exists"

            if reg_type == "200" or reg_resp["status"] == 503:
                reg_token_raw = reg_json.get("description", "") if reg_type == "200" else ""
                used_proxy = proxy
                used_proxy_id = proxy_id
                progress(f"register SUCCESS token={reg_token_raw}")
                try:
                    saved_cookies_full = s.get_all_cookies()
                except Exception:
                    saved_cookies_full = []
                try: s.close()
                except Exception: pass
                proxy_success = True
                break

            # 400, not 80. The portal states its refusal in full Portuguese sentences, and at 80
            # chars every one of them was cut mid-word: "De forma a melhor garantir a
            # possibilidade de agendamentos para todos os utiliza" -- which reads as a fair-use
            # notice but never reaches the part that says WHICH limit was hit or for how long.
            # Diagnosing the 2026-08-21 registration stoppage came down to guessing from a
            # truncated sentence; same class as the 24-char proxies.health column.
            reg_result_detail = f"type={reg_type} desc={reg_desc[:400]}"
            progress(f"register attempt failed ({reg_result_detail})")
            try: s.close()
            except Exception: pass

            # STOP on a reCAPTCHA rejection. Do not spend another attempt.
            #
            # The portal states the rule itself, and we only saw it once the detail stopped being
            # truncated at 80 chars (2026-08-21):
            #
            #   "o sistema apenas permite falhar o processo de ReCaptcha 2 vezes. Se tentar
            #    executar este processo mais vezes no periodo de tempo definido o seu acesso a
            #    plataforma de E-VISA sera bloqueado"
            #
            #   -- the system allows the reCAPTCHA process to fail TWICE; try more within the
            #      window and your access to the platform is BLOCKED.
            #
            # MAX_PROXY_RETRIES is 5, so one sub-account could produce five reCAPTCHA failures --
            # 2.5x the threshold -- and a batch of six ran concurrently. This retry policy was not
            # failing to register; it was actively working toward getting the platform access
            # blocked, and "Dispoe de mais 0 tentativas" (0 attempts remaining) was the counter
            # telling us so.
            #
            # A proxy/network failure is still worth another proxy (those `continue`s above are
            # untouched -- they never reached the portal's CAPTCHA check). A reCAPTCHA REJECTION is
            # the portal's verdict on the token, and repeating it is the one thing it warns about.
            if is_recaptcha_rejection(str(reg_type), reg_desc):
                progress("STOPPING: portal allows only 2 reCAPTCHA failures before blocking access "
                         "-- not retrying this sub-account")
                result("failed", detail=reg_result_detail)
                return "failed"

    if not proxy_success:
        progress("FAIL exhausted proxy retries")
        result("failed", detail=reg_result_detail or "exhausted proxy retries")
        return "failed"

    # solver_used is hardcoded: race_all is only ever invoked here with capsolver_keys (anticaptcha/
    # twocaptcha/capmonster are always passed empty) -- update this if that call ever wires up
    # additional solvers, since race_all itself doesn't report back which service won.
    result("registered", email=email_addr, email_pass=email_pass,
           proxy_id=used_proxy_id, solver_used="capsolver")

    # ── poll inbox, then verify ──────────────────────────────────────────────
    # async_poll_for_tokens can legitimately run for up to EMAIL_POLL_TIMEOUT (240s) with nothing to
    # report -- without this line the Live column would show stale "submitting registration" text
    # for up to 4 minutes with no indication anything is still happening.
    progress(f"waiting for verification email (up to {EMAIL_POLL_TIMEOUT}s)")
    tokens = await mail.async_poll_for_tokens(email_key, timeout=EMAIL_POLL_TIMEOUT, interval=5)
    if not tokens:
        progress("FAIL no email tokens received")
        result("failed", detail="verify_email_timeout")
        return "failed"

    link_token = tokens.get("link") or ""
    code_token = tokens.get("code") or ""
    if link_token.upper().startswith("3D") and len(link_token) == 34:
        link_token = link_token[2:]
    token_search = reg_token_raw or link_token
    token_input = code_token or (
        f"{token_search[0:8]}-{token_search[8:12]}-{token_search[12:16]}-"
        f"{token_search[16:20]}-{token_search[20:]}" if len(token_search) == 32 else token_search
    )
    token_page = f"{AUTH_URL}?token={token_search}"

    progress("email tokens received -- verifying")
    verified = False
    for verify_attempt in range(MAX_VERIFY_RETRIES):
        progress(f"verifying (attempt {verify_attempt + 1}/{MAX_VERIFY_RETRIES})")
        if verify_attempt == 0:
            verify_proxy, cookies_full = used_proxy, saved_cookies_full
        else:
            verify_proxy, cookies_full = next(proxies_cycle).get("url"), []
        cookies = {c["name"]: c["value"] for c in cookies_full if "name" in c and "value" in c}

        async with browser_sem:
            sv = None
            try:
                def _open(p=verify_proxy, ck=cookies, ckf=cookies_full):
                    return get_session(p, inject_cookies=ck, inject_cookies_full=ckf, headless=True)
                sv = await loop.run_in_executor(executor, _open)

                def _verify(sv=sv):
                    return sv.verify_email(token_url=token_page, insert_jsp=INSERT_TOKEN_JSP,
                                           verify_url=VERIFY_URL, token_input=token_input,
                                           token_search=token_search, lang=lang, timeout=90)
                vr = await loop.run_in_executor(executor, _verify)
            except Exception as e:
                progress(f"verify attempt {verify_attempt + 1} failed: {e}")
                continue
            finally:
                if sv is not None:
                    try: sv.close()
                    except Exception: pass

            vr_status = vr.get("status", 0)
            vr_body = (vr.get("body") or "").strip()
            if vr_status == 200:
                try:
                    vres = json.loads(vr_body)
                    verified = vres.get("type", "") != "error"
                except Exception:
                    # Ported verbatim from storm-auto's proven classification (batch_register.py's
                    # _register_one): no parseable JSON but also no DataDome challenge marker means
                    # a clean HTML redirect -- treated as verified there too. Trust the same
                    # result-assessment storm-auto's real usage already validated, not a stricter
                    # bar invented here.
                    verified = "/ch/bd.js" not in vr_body
            progress(f"verify attempt {verify_attempt + 1}: status={vr_status} verified={verified}")
        if verified:
            break

    if not verified:
        progress("FAIL verify exhausted attempts")
        result("failed", detail="verify_failed")
        return "failed"

    progress("VERIFIED")
    result("verified")
    return "verified"


async def _run_all(bus: Bus, run_id: str, accounts: list[dict], proxies: list[dict],
                   capsolver_keys: list[str], lang: str, email_provider: str, email_cfg: dict,
                   concurrency: int, browser_concurrency: int) -> None:
    proxies_cycle = itertools.cycle(proxies or [{"id": None, "url": None}])
    sem = asyncio.Semaphore(max(1, concurrency))
    browser_sem = asyncio.Semaphore(max(1, browser_concurrency))
    executor = ThreadPoolExecutor(max_workers=max(1, concurrency) * 2 + 10)

    async def bounded(acct: dict) -> str:
        async with sem:
            return await _register_one(acct, proxies_cycle, capsolver_keys, lang, email_provider,
                                       email_cfg, executor, bus, run_id, browser_sem)

    results = await asyncio.gather(*(bounded(a) for a in accounts), return_exceptions=True)
    counts: dict[str, int] = {}
    for r in results:
        k = f"error:{r.__class__.__name__}" if isinstance(r, Exception) else r
        counts[k] = counts.get(k, 0) + 1
    bus.emit_event(run_id, "job_terminal", counts=counts)
    executor.shutdown(wait=False)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m apps.registrar <run_id>", file=sys.stderr)
        return 2
    run_id = sys.argv[1]
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    bus = Bus(connect(redis_url))
    consumer = f"registrar-{os.getpid()}"
    signals = BusSignals(bus, run_id, consumer)
    bus.emit_event(run_id, "process_up", pid=os.getpid())

    cfg = signals.wait_launch()
    if cfg is None:
        bus.emit_event(run_id, "job_terminal", counts={}, note="stopped before launch")
        return 0

    payload = signals.launch_payload
    accounts = payload.get("accounts", [])
    proxies = payload.get("proxies") or [{"id": None, "url": None}]
    capsolver_keys = payload.get("capsolver_keys", [])
    lang = payload.get("lang", "PT")
    email_provider = payload.get("email_provider", "mailtm")
    email_cfg = payload.get("email_cfg", {})
    concurrency = int(payload.get("concurrency", 10))
    browser_concurrency = int(payload.get("browser_concurrency", concurrency))

    if not accounts:
        bus.emit_event(run_id, "job_terminal", counts={})
        return 0

    asyncio.run(_run_all(bus, run_id, accounts, proxies, capsolver_keys, lang,
                         email_provider, email_cfg, concurrency, browser_concurrency))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
