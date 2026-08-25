#!/usr/bin/env python3
"""Q16: does an authenticated portal session survive a proxy exit/IP change?

WHY THIS EXISTS. Q1 measured that traffic does not preserve a proxy exit. Whether that MATTERS
depends entirely on one unknown: if the portal binds its authenticated session to the requesting IP,
an exit move kills the session and exit continuity must be managed (Option E). If it does not, the
hold can be decoupled from the exit entirely (Option F).

The only evidence we have is inherited, not measured here: storm-auto's `_swap_to_hold_pool` asserts
"booking form-state is server-side / Vistos_sid-keyed -> transfers to a fresh browser+IP while alive
-- validated 0724", and its whole holding architecture depends on it. storm-hub implements the same
migration in LiveEngineOps.do_swap() and has NEVER executed it once in production.

This probe is do_swap performed deliberately, once, against the existing _probe_alive() check.

HARD BOUNDARY -- this experiment cannot book anything:
  it calls do_login, do_warmup, _probe_alive, get_all_cookies, get_session, browser_nav ONLY.
  It never calls do_apply, never POSTs ScheduleController, never touches SubmeterVistoCriaPDF or
  MostrarPdf. WARMUP_ONLY and no_book are forced on as defence in depth. There is no code path from
  here to a submit.

It also creates NO ProxyLease rows (this project already leaks 107 of them) and changes no
production configuration: do_swap stays off, holding is untouched, proxy selection is untouched.

Cookie SECRETS are never persisted. The result file records cookie NAMES, a count, and a truncated
SHA-256 of the session cookie's value -- enough to prove the same session material moved, revealing
nothing usable.

    .venv/bin/python probes/session_ip_migration_probe.py --pairs 1
    .venv/bin/python probes/session_ip_migration_probe.py --report var/q16_session_migration.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IP_ECHO = "https://api.ipify.org"
_SESSION_COOKIE_HINTS = ("vistos_sid", "jsessionid")


def _sha12(v: str) -> str:
    return hashlib.sha256((v or "").encode()).hexdigest()[:12]


def _exit_ip_via_browser(session, timeout: int = 30) -> str | None:
    """Read the exit address THROUGH THE BROWSER, never a raw primp probe.

    CONFIRMED_FACTS.md #4: raw-probing an exit you intend to browse from poisons it (83% -> 0%).
    A browser navigation is ordinary browser traffic and does not. The page is left on the echo's
    origin, which is fine here because every caller navigates somewhere real immediately after.
    """
    try:
        session.browser_nav(IP_ECHO, timeout=timeout)
        body = (session.browser_eval("document.body ? document.body.innerText : ''") or "").strip()
    except Exception:
        return None
    return body if body and len(body) <= 45 and " " not in body else None


def _classify_probe(session, sched_url: str) -> tuple[str, dict]:
    """Navigate the authenticated endpoint on P2 and classify. Distinguishes transport failure,
    portal unavailability, a WAF challenge, an auth redirect, and a live authenticated page --
    which the boolean _probe_alive() alone cannot do."""
    from apps.worker.ops_live import (_SCHED_PAGE_MARKERS, _is_challenge_body, _is_portal_busy)
    detail: dict = {}
    try:
        final_url = session.browser_nav(sched_url, timeout=45)
    except Exception as e:
        detail["error"] = f"{type(e).__name__}: {e}"[:200]
        return "P2_TRANSPORT_FAIL", detail
    detail["final_url"] = final_url
    try:
        html = session.browser_eval("document.documentElement.outerHTML") or ""
    except Exception as e:
        detail["error"] = f"{type(e).__name__}: {e}"[:200]
        return "INCONCLUSIVE", detail
    detail["page_bytes"] = len(html)
    if _is_challenge_body(html):
        return "P2_WAF_CHALLENGE", detail
    if _is_portal_busy(html):
        return "PORTAL_BUSY", detail
    # The portal has a DEDICATED endpoint for a server-side session that no longer exists. It is
    # not an auth redirect and was falling through to INCONCLUSIVE. storm-auto hit the same page
    # during its swaps and attributed it to the form-state ageing out during the browser-open
    # window -- i.e. elapsed TIME -- not to the address change, which is why this is reported
    # separately from an auth redirect rather than merged into one "session died" bucket.
    if "sessionLost" in (final_url or ""):
        return "SESSION_GONE_SESSIONLOST", detail
    if "Authentication.jsp" in (final_url or ""):
        return "SESSION_INVALIDATED", detail
    hits = [m for m in _SCHED_PAGE_MARKERS if m in html]
    detail["markers"] = hits
    if "cmbPeriodo" in html or "getPeriodosOcupados" in html:
        return "SESSION_SURVIVES", detail
    return "INCONCLUSIVE", detail


def run_pair(db, bk, identity, subs, p1_row, p2_row, geo: str, headless: bool, log) -> dict:
    from libs.domain.enums import SettingsLayer as L
    from libs.domain.settings.resolver import resolve
    from libs.engine.config import EngineConfig
    from libs.engine import vendor_env
    from libs.proxies.lease import conn_url, session_id
    from apps.hub.settings_store import get_global
    from apps.hub.main import _live_account
    from libs.db import models as M
    from sqlalchemy import select

    rec: dict = {"experiment_id": str(uuid.uuid4())[:8], "ts": time.time(),
                 "identity_id": identity.id, "booking": bk.name}

    eff = resolve({L.GLOBAL: get_global(db), L.BOOKING: (bk.settings_json or {})})
    eff["ROLE"] = "apply"
    # Defence in depth: even though nothing here calls do_apply, make a submit impossible to reach
    # through configuration too.
    eff["WARMUP_ONLY"] = True
    eff["CAPTURE_ONLY"] = True
    # Transient failures are DEAD EXITS, rolled rather than retried, and are never charged to the
    # sub-account's strike budget -- so a larger budget here buys reachability, not account burn.
    # STICKY_RETRY_MAX is deliberately left alone: that one IS the account budget.
    eff["LOGIN_TRANSIENT_RETRY_MAX"] = 10
    cfg = EngineConfig.from_settings(eff, no_book=True)
    assert cfg.no_book and cfg.warmup_only, "safety: no_book/warmup_only must be set"

    # ORDERING: the vendored engine reads its anti-detect knobs into module constants at IMPORT
    # time, so this must run BEFORE ops_live is imported (apps/worker/__main__.py does the same).
    # Skipping it is what condemned 639 proxies with stealth off.
    vendor_env.apply(cfg.raw)
    from apps.worker.ops_live import LiveEngineOps, SCHED_JSP
    from libs.engine.vendor import get_session

    p1 = conn_url(p1_row, geo=geo)
    p2 = conn_url(p2_row, geo=geo)
    rec["p1"] = {"provider": p1_row.provider, "kind": p1_row.kind,
                 "endpoint": p1_row.endpoint, "session_id": session_id(p1), "geo": geo}
    rec["p2"] = {"provider": p2_row.provider, "kind": p2_row.kind,
                 "endpoint": p2_row.endpoint, "session_id": session_id(p2), "geo": geo}

    caps = [k.strip() for k in (get_global(db).get("CAPSOLVER_KEYS", "") or "").split(",") if k.strip()]
    quest = db.scalar(select(M.AppConfig).where(M.AppConfig.key == "quest_steps"))
    formd = db.scalar(select(M.AppConfig).where(M.AppConfig.key == "form_defaults"))
    accounts = [dict(_live_account(identity, sa), sub_account_id=sa.id) for sa in subs]

    ops = LiveEngineOps(
        cfg, account=_live_account(identity, subs[0]), posto_id=str(bk.posto_id),
        nationality=bk.nationality, residence=bk.residence,
        quest_steps=(quest.value if quest else {}), form_defaults=(formd.value if formd else {}),
        capsolver_keys=caps, warm_proxy=p1, headless=headless,
        sub_account_id=subs[0].id, accounts=accounts, identity_id=identity.id,
    )
    ops._log = log

    sched_url = SCHED_JSP + "?posto_id=" + str(bk.posto_id)
    p2_session = None
    try:
        # ── Phase A: authenticate + warm on P1 ────────────────────────────────
        log(f"[{rec['experiment_id']}] Phase A: login on P1 #{rec['p1']['session_id']} geo={geo}")
        t0 = time.time()
        if not ops.do_login():
            rec.update(result="INVALID_LOGIN_FAILED", phase="A",
                       note="never counted -- a failed login says nothing about session/IP binding")
            return rec
        rec["login_secs"] = round(time.time() - t0, 1)
        rec["sub_account_used"] = ops.sub_account_id
        log(f"[{rec['experiment_id']}] login OK in {rec['login_secs']}s")

        if not ops.do_warmup():
            rec.update(result="INVALID_WARMUP_FAILED", phase="A")
            return rec
        if not ops._probe_alive():
            rec.update(result="INVALID_P1_NOT_AUTHENTICATED", phase="A")
            return rec
        rec["p1_authenticated"] = True
        rec["posto_pdf_anchor"] = bool(ops.posto_pdf)
        log(f"[{rec['experiment_id']}] P1 authenticated + warmed")

        # ── Phase B: capture ──────────────────────────────────────────────────
        cookies_full = ops.session.get_all_cookies()
        cookies = {c["name"]: c["value"] for c in cookies_full if "name" in c and "value" in c}
        sid_name = next((n for n in cookies if n.lower() in _SESSION_COOKIE_HINTS), None)
        rec["cookies"] = {
            "count": len(cookies_full),
            "names": sorted({c.get("name", "") for c in cookies_full}),
            "session_cookie_name": sid_name,
            # Proves the SAME session material moved, without persisting anything usable.
            "session_cookie_sha12": _sha12(cookies.get(sid_name, "")) if sid_name else None,
        }
        rec["p1"]["exit_ip"] = _exit_ip_via_browser(ops.session)
        log(f"[{rec['experiment_id']}] captured {len(cookies_full)} cookies; P1 exit={rec['p1']['exit_ip']}")

        # ── Phase C: migrate to P2 ────────────────────────────────────────────
        # A dead P2 exit is a TRANSPORT fact and must never be read as session invalidation
        # (spec section 5). The established remedy is the one production already uses: roll the
        # sticky session id rather than retry the same dead exit (CONFIRMED_FACTS.md #3, 25%->83%).
        # Never raw-probe P2 first -- that poisons the exit we are about to browse from (#4).
        from libs.proxies.lease import roll_sticky_session
        capture_at = time.time()
        p2_attempts = []
        for attempt in range(1, 5):
            try:
                p2_session = get_session(proxy=p2, inject_cookies=cookies,
                                         inject_cookies_full=cookies_full, headless=headless,
                                         fp_seed=ops.account.get("username"))
            except Exception as e:
                p2_attempts.append({"session_id": session_id(p2), "error": f"{type(e).__name__}"})
                log(f"[{rec['experiment_id']}] P2 #{session_id(p2)} unreachable "
                    f"({type(e).__name__}) -- rolling to a fresh exit ({attempt}/4)")
                rolled = roll_sticky_session(p2)
                if rolled == p2:
                    break
                p2 = rolled
                continue
            ip = _exit_ip_via_browser(p2_session)
            if ip is None:
                p2_attempts.append({"session_id": session_id(p2), "error": "no exit ip"})
                try:
                    p2_session.close_browser()
                except Exception:
                    pass
                p2_session = None
                rolled = roll_sticky_session(p2)
                if rolled == p2:
                    break
                p2 = rolled
                continue
            rec["p2"]["session_id"] = session_id(p2)
            rec["p2"]["exit_ip"] = ip
            break
        rec["p2_attempts"] = p2_attempts
        # How long the authenticated session sat between capture and probe. Recorded because the
        # server-side form state has a short TTL: a long gap could make a LIVE session look
        # inconclusive for reasons that have nothing to do with the IP change.
        rec["migration_gap_secs"] = round(time.time() - capture_at, 1)
        log(f"[{rec['experiment_id']}] P2 exit={rec['p2'].get('exit_ip')} "
            f"(gap {rec['migration_gap_secs']}s, {len(p2_attempts)} dead exit(s) rolled past)")
        if p2_session is None:
            rec.update(result="INVALID_P2_UNREACHABLE", phase="C",
                       note="every P2 candidate was dead at transport level -- says nothing "
                            "about session/IP binding")
            return rec

        # §4/§5: the experiment is only valid if the exits actually differ, and a dead P2 must never
        # be read as session invalidation.
        if rec["p2"]["exit_ip"] is None:
            rec.update(result="INVALID_P2_UNREACHABLE", phase="C",
                       note="P2 could not carry a request at all -- transport, not session")
            return rec
        if rec["p1"]["exit_ip"] and rec["p1"]["exit_ip"] == rec["p2"]["exit_ip"]:
            rec.update(result="INVALID_SAME_EXIT", phase="C",
                       note="no IP change occurred -- nothing was tested")
            return rec
        rec["exit_changed"] = True

        # ── Phase D: classify ─────────────────────────────────────────────────
        verdict, detail = _classify_probe(p2_session, sched_url)
        rec["probe_detail"] = detail
        rec["result"] = verdict
        # Canonical cross-check with the production helper, on the migrated session.
        try:
            old, ops.session = ops.session, p2_session
            rec["probe_alive_agrees"] = bool(ops._probe_alive())
        finally:
            ops.session = old
        log(f"[{rec['experiment_id']}] VERDICT {verdict}  (_probe_alive={rec.get('probe_alive_agrees')})")
        return rec
    except Exception as e:
        rec.update(result="INVALID_EXCEPTION", error=f"{type(e).__name__}: {e}"[:300])
        return rec
    finally:
        for s in (p2_session, ops.session):
            try:
                if s is not None:
                    s.close_browser()
            except Exception:
                pass


def run_from_checkpoint(db, bk, ck_path: Path, provider: str, geo: str, headless: bool, log) -> dict:
    """The zero-login variant of the same question.

    A checkpoint is an authenticated cookie jar captured on SOME proxy at SOME past moment. Loading
    it into a browser on a FRESH proxy is precisely the migration under test -- and it costs no
    login, no CAPTCHA solve, and no sub-account strike.

    The result is ASYMMETRIC and must be read that way:
      * authenticated  -> DECISIVE. The session survived both an exit change and a browser change,
        so it cannot be bound to the establishing IP.
      * not authenticated -> NO INFORMATION. An old jar may simply have expired, and this design
        cannot separate portal-side expiry from IP invalidation.
    So a negative here is never evidence for IP binding, and is recorded as INCONCLUSIVE.
    """
    from libs.domain.enums import SettingsLayer as L
    from libs.domain.settings.resolver import resolve
    from libs.engine.config import EngineConfig
    from libs.engine import vendor_env
    from libs.proxies.lease import conn_url, session_id, roll_sticky_session
    from apps.hub.settings_store import get_global
    from libs.db import models as M
    from sqlalchemy import select
    import random as _rnd

    data = json.loads(ck_path.read_text())
    age_h = (time.time() - float(data.get("saved_at") or 0)) / 3600.0
    rec = {"experiment_id": str(uuid.uuid4())[:8], "ts": time.time(), "mode": "checkpoint",
           "identity_id": data.get("identity_id"), "booking": bk.name,
           "checkpoint": {"age_hours": round(age_h, 2), "stage": data.get("stage"),
                          "posto_pdf": data.get("posto_pdf")}}

    eff = resolve({L.GLOBAL: get_global(db), L.BOOKING: (bk.settings_json or {})})
    eff["ROLE"] = "apply"; eff["WARMUP_ONLY"] = True; eff["CAPTURE_ONLY"] = True
    cfg = EngineConfig.from_settings(eff, no_book=True)
    vendor_env.apply(cfg.raw)
    from apps.worker.ops_live import SCHED_JSP
    from libs.engine.vendor import get_session

    cookies = data.get("cookies") or {}
    cookies_full = data.get("cookies_full") or []
    sid_name = next((n for n in cookies if n.lower() in _SESSION_COOKIE_HINTS), None)
    rec["cookies"] = {"count": len(cookies_full), "names": sorted(cookies.keys()),
                      "session_cookie_name": sid_name,
                      "session_cookie_sha12": _sha12(cookies.get(sid_name, "")) if sid_name else None}
    if not cookies:
        rec["result"] = "INVALID_EMPTY_CHECKPOINT"
        return rec

    pool = db.scalars(select(M.Proxy).where(
        M.Proxy.provider == provider, M.Proxy.active.is_(True)).limit(2000)).all()
    _rnd.shuffle(pool)
    url = conn_url(pool[0], geo=geo)
    sess = None
    for attempt in range(1, 5):
        try:
            sess = get_session(proxy=url, inject_cookies=cookies, inject_cookies_full=cookies_full,
                               headless=headless, fp_seed=data.get("sub_account_id"))
        except Exception as e:
            log(f"[{rec['experiment_id']}] exit #{session_id(url)} dead ({type(e).__name__}) -- rolling")
            url = roll_sticky_session(url) if roll_sticky_session(url) != url else conn_url(pool[attempt], geo=geo)
            continue
        ip = _exit_ip_via_browser(sess)
        if ip is None:
            try: sess.close_browser()
            except Exception: pass
            sess = None
            url = roll_sticky_session(url) if roll_sticky_session(url) != url else conn_url(pool[attempt], geo=geo)
            continue
        rec["p2"] = {"provider": provider, "kind": pool[0].kind, "endpoint": pool[0].endpoint,
                     "session_id": session_id(url), "geo": geo, "exit_ip": ip}
        break
    if sess is None:
        rec["result"] = "INVALID_NO_REACHABLE_EXIT"
        return rec

    # The originating exit is not recorded in the checkpoint, but after this much elapsed time it is
    # certainly gone: soax holds an exit at most sessionlength (60 min, measured 20/20) and webshare
    # loses one at 0.278/session-hour. A jar this old cannot still be on its establishing address.
    rec["exit_certainly_changed"] = age_h > 1.0
    log(f"[{rec['experiment_id']}] checkpoint age {age_h:.1f}h -> exit {rec['p2']['exit_ip']}")
    try:
        verdict, detail = _classify_probe(sess, SCHED_JSP + "?posto_id=" + str(bk.posto_id))
        rec["probe_detail"] = detail
        # A negative can never be evidence FOR IP binding here -- see the docstring.
        rec["result"] = verdict if verdict == "SESSION_SURVIVES" else f"INCONCLUSIVE_{verdict}"
        log(f"[{rec['experiment_id']}] VERDICT {rec['result']}")
    finally:
        try: sess.close_browser()
        except Exception: pass
    return rec


def report(path: str) -> int:
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    print(f"\n=== Q16: authenticated session across a proxy exit change -- {len(rows)} run(s) ===\n")
    valid = [r for r in rows if not str(r.get("result", "")).startswith("INVALID")]
    for r in rows:
        p1, p2 = r.get("p1", {}), r.get("p2", {})
        print(f"  [{r.get('experiment_id')}] {r.get('result')}")
        print(f"      P1 {p1.get('provider')}/{p1.get('kind')}#{p1.get('session_id')} exit={p1.get('exit_ip')}")
        print(f"      P2 {p2.get('provider')}/{p2.get('kind')}#{p2.get('session_id')} exit={p2.get('exit_ip')}")
        if r.get("cookies"):
            c = r["cookies"]
            print(f"      cookies={c['count']} session={c['session_cookie_name']} sha12={c['session_cookie_sha12']}")
        if r.get("probe_detail"):
            d = r["probe_detail"]
            print(f"      page={d.get('page_bytes')}B markers={d.get('markers')} url={str(d.get('final_url'))[:80]}")
        if r.get("error"):
            print(f"      error={r['error']}")
        if r.get("note"):
            print(f"      note={r['note']}")
    print(f"\n  valid runs: {len(valid)}/{len(rows)}")
    if valid:
        from collections import Counter
        print("  verdicts:", dict(Counter(r["result"] for r in valid)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", type=int, default=1, help="independent P1->P2 pairs (each needs a login)")
    ap.add_argument("--geo", default="de", help="exit country (DE/IT measured best for login)")
    ap.add_argument("--provider", default="soax", choices=["soax", "webshare"],
                    help="provider for BOTH P1 and P2 (same contract isolates the exit as the variable)")
    ap.add_argument("--booking", default="Test 2032", help="booking to source posto/nationality from")
    ap.add_argument("--out", default="var/q16_session_migration.jsonl")
    ap.add_argument("--report", help="re-report an existing result file and exit")
    ap.add_argument("--from-checkpoint", type=int, default=0, metavar="N",
                    help="zero-login variant: try the N freshest checkpoints (a positive result is "
                         "decisive; a negative one is uninformative)")
    ap.add_argument("--no-headless", action="store_true")
    args = ap.parse_args()
    if args.report:
        return report(args.report)

    from sqlalchemy import select
    from apps.hub.db import SessionLocal
    from libs.db import models as M
    from libs.domain.enums import SubAccountStatus
    from apps.hub.main import _rank_live_sub_accounts

    def log(t):
        print(f"  {time.strftime('%H:%M:%S')} {t}", flush=True)

    db = SessionLocal()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        bk = db.scalar(select(M.Booking).where(M.Booking.name == args.booking))
        if bk is None:
            print(f"no booking named {args.booking!r}")
            return 1
        # SAME provider on both sides: identical contract, so the ONLY difference between P1 and P2
        # is the exit address -- exactly the variable under test. soax by default because portal
        # reachability measured 7/8 against webshare's 2/8, and this experiment needs a successful
        # login before it can test anything at all.
        pool = db.scalars(select(M.Proxy).where(
            M.Proxy.provider == args.provider, M.Proxy.active.is_(True)).limit(2000)).all()
        import random as _rnd
        _rnd.shuffle(pool)   # never reuse the same session ids across runs of this probe
        if args.from_checkpoint:
            cks = sorted(Path("var/checkpoints").glob("*.json"),
                         key=lambda f: json.loads(f.read_text()).get("saved_at", 0), reverse=True)
            for ck in cks[:args.from_checkpoint]:
                rec = run_from_checkpoint(db, bk, ck, args.provider, args.geo,
                                          not args.no_headless, log)
                with out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
                print(f"--- checkpoint {ck.stem[:8]}: {rec.get('result')} ---")
                if rec.get("result") == "SESSION_SURVIVES":
                    log("decisive positive -- stopping, no further logins needed")
                    break
            return report(str(out))

        used_identities: set[str] = set()
        results = []
        for i in range(args.pairs):
            ident = None
            _cands = db.scalars(select(M.Identity).where(
                M.Identity.status == "active").limit(200)).all()
            # Random, not first-in-order: repeated invocations were re-picking the SAME three
            # sub-accounts every time, piling attempts onto accounts the portal may already be
            # rate-limiting -- which confounds the throttle signal and wastes the account budget.
            import random as _r0; _r0.shuffle(_cands)
            for cand in _cands:
                if cand.id in used_identities:
                    continue
                subs = [s for s in _rank_live_sub_accounts(db, cand.id)
                        if s.status != SubAccountStatus.BURNED]
                if subs:
                    ident, ident_subs = cand, subs
                    break
            if ident is None:
                print("no identity with usable sub-accounts left")
                break
            used_identities.add(ident.id)
            p1_row, p2_row = pool[i * 2], pool[i * 2 + 1]
            print(f"\n--- pair {i+1}/{args.pairs}: identity {ident.full_name!r} "
                  f"({len(ident_subs)} sub-accounts) ---")
            rec = run_pair(db, bk, ident, ident_subs, p1_row, p2_row, args.geo,
                           not args.no_headless, log)
            results.append(rec)
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            print(f"--- pair {i+1} result: {rec.get('result')} ---")
    finally:
        db.close()
    return report(str(out))


if __name__ == "__main__":
    raise SystemExit(main())
