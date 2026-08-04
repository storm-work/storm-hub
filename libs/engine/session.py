"""Browser session — synchronous Playwright per worker process (clean rewrite of storm-auto session).

Because each storm-hub worker is its OWN OS process, we run Playwright synchronously here and drop
storm-auto's in-process thread+command-queue model entirely. Methods port the CONFIRMED behavior:
  - launch with stealth args + proxy
  - nav (HOME->AUTH) with the LOGIN_FIELD_RETRIES form-render wait
  - login: type creds -> capsolver token -> inject (textarea + grecaptcha.getResponse patch +
    onCaptchaSuccess) -> synthetic POST /login form -> read result (HAR-confirmed success signature)

The first live check of this file (a real login on a SOAX proxy + capsolver key) is operator-run,
since it makes genuine login attempts against the portal.
"""
from __future__ import annotations

import json
import random
import re
import time

from . import solver
from .stealth import STEALTH_SCRIPT, sec_ch_ua_headers

BASE = "https://pedidodevistos.mne.gov.pt"
HOME_URL = BASE + "/VistosOnline/"
AUTH_URL = BASE + "/VistosOnline/Authentication.jsp"
LOGIN_URL = BASE + "/VistosOnline/login"

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
]
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/128.0.0.0 Safari/537.36")


def _split_proxy(conn_url: str | None) -> dict | None:
    """http://user:pass@host:port -> Playwright proxy dict {server, username, password}."""
    if not conn_url:
        return None
    m = re.match(r"^(https?)://(?:([^:@/]+):([^@/]+)@)?([^:/@]+):(\d+)/?$", conn_url)
    if not m:
        return None
    scheme, user, pw, host, port = m.groups()
    px = {"server": f"{scheme}://{host}:{port}"}
    if user:
        px["username"], px["password"] = user, pw
    return px


class BrowserSession:
    def __init__(self, proxy: str | None = None, *, headless: bool = True,
                 nav_timeout_scale: float = 1.0, field_retries: int = 3):
        self.proxy = proxy
        self.headless = headless
        self.nav_scale = max(1.0, nav_timeout_scale)
        self.field_retries = max(1, field_retries)
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        # NEW headless (--headless=new) is far less detectable than old headless, which DataDome
        # WAF-blocks on the auth page. headless=False + the flag = real render path, no window.
        args = list(_LAUNCH_ARGS)
        launch_headless = self.headless
        if self.headless:
            launch_headless = False
            args.append("--headless=new")
        self._browser = self._pw.chromium.launch(
            headless=launch_headless, args=args, proxy=_split_proxy(self.proxy),
        )
        self._context = self._browser.new_context(
            user_agent=_UA, locale="en-US", timezone_id="Europe/Lisbon",
            viewport={"width": 1366, "height": 768}, ignore_https_errors=True,
            extra_http_headers=sec_ch_ua_headers(),
        )
        self._context.add_init_script(STEALTH_SCRIPT)   # coherent fingerprint (webdriver/UAD/plugins/webgl/…)
        self.page = self._context.new_page()
        return self

    def close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                obj and obj.close()
            except Exception:
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ── navigation ────────────────────────────────────────────────────────────
    def nav(self, url: str, timeout: int = 30, wait_until: str = "domcontentloaded") -> str:
        self.page.goto(url, timeout=int(timeout * 1000 * self.nav_scale), wait_until=wait_until)
        return self.page.url

    def eval(self, js: str):
        return self.page.evaluate(js)

    def page_diag(self) -> dict:
        """Snapshot of the current page for diagnosing why the login form didn't render."""
        try:
            html = self.page.content() or ""
        except Exception:
            html = ""
        low = html.lower()
        try:
            title = self.page.title()
        except Exception:
            title = ""
        try:
            inputs = self.page.evaluate(
                "() => Array.from(document.querySelectorAll('input,iframe')).map(e => "
                "({tag:e.tagName, name:e.name||'', type:e.type||'', id:e.id||'', "
                "title:(e.getAttribute('title')||'').slice(0,40)}))"
            )
        except Exception:
            inputs = []
        return {
            "title": title, "html_len": len(html),
            "markers": [m for m in ("datadome", "/ch/bd.js", "recaptcha", "handheldfriendly",
                                    "request could not be processed", "authentication.jsp")
                        if m in low],
            "fields": inputs[:15],
            "snippet": re.sub(r"\s+", " ", html)[:400],
        }

    # ── login (confirmed sequence) ────────────────────────────────────────────
    def _nav_to_login_form(self) -> bool:
        """HOME -> AUTH, re-navigating until the username field is actually visible (slow-portal
        tolerance, ported from the LOGIN_FIELD_RETRIES fix). Both navs are retried — a slow/dead
        proxy or a stalled AUTH nav must NOT crash the login; it retries then gives up cleanly."""
        try:
            self.page.goto(HOME_URL, timeout=int(30000 * self.nav_scale), wait_until="domcontentloaded")
        except Exception:
            pass   # HOME bootstrap is best-effort; AUTH nav below is what matters
        sel = 'input[name="username"], input#username, input[autocomplete="username"]'
        for attempt in range(1, self.field_retries + 1):
            try:
                self.page.goto(AUTH_URL, timeout=int(25000 * self.nav_scale),
                               wait_until="domcontentloaded", referer=HOME_URL)
                self.page.wait_for_selector(sel, timeout=int(15000 * self.nav_scale), state="visible")
                return True
            except Exception:
                time.sleep(random.uniform(1.5, 3.0))
        return False

    def _has_datadome_cookie(self) -> bool:
        try:
            return any(c["name"].lower() == "datadome" for c in self._context.cookies())
        except Exception:
            return False

    def _settle_datadome(self, timeout: float = 20.0) -> bool:
        """Wait for DataDome's bd.js challenge to run and set the `datadome` cookie before we POST
        /login — a POST with an immature cookie gets 'Request could not be processed'."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._has_datadome_cookie():
                return True
            self.page.wait_for_timeout(1000)
            try:
                self.page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
        return self._has_datadome_cookie()

    def _type_credentials(self, username: str, password: str) -> None:
        page = self.page
        try:
            uf = page.locator('input[name="username"], input#username, '
                              'input[autocomplete="username"], input[type="text"]:visible').first
            uf.click(timeout=int(5000 * self.nav_scale))
            uf.fill("")
            uf.type(username, delay=random.randint(70, 150))
        except Exception:
            pass
        try:
            pf = page.locator('input[name="password"], input#password, '
                              'input[type="password"]:visible').first
            pf.click(timeout=int(5000 * self.nav_scale))
            pf.fill("")
            pf.type(password, delay=random.randint(70, 150))
        except Exception:
            pass

    def _inject_token(self, token: str) -> None:
        tok = json.dumps(token)
        # 1) set the textarea value
        self.page.evaluate("""(() => { const _t=%s;
            for (const s of ['#g-recaptcha-response-1','#g-recaptcha-response']) {
                const ta=document.querySelector(s); if(!ta) continue;
                const set=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value')?.set;
                set?set.call(ta,_t):ta.value=_t;
                ta.dispatchEvent(new Event('input',{bubbles:true}));
                ta.dispatchEvent(new Event('change',{bubbles:true})); } })()""" % tok)
        # 2) patch grecaptcha(.enterprise).getResponse to return the token
        self.page.evaluate("""(() => { const _t=%s;
            const p=o=>{ if(!o)return; const g=o.getResponse;
              o.getResponse=function(i){ const r=g?g.call(this,i):''; return r||_t; }; };
            if(window.grecaptcha){ p(window.grecaptcha); if(window.grecaptcha.enterprise) p(window.grecaptcha.enterprise);} })()""" % tok)
        # 3) call the portal's onCaptchaSuccess callback if present
        try:
            self.page.evaluate("""(() => { const _t=%s;
                if(typeof window.onCaptchaSuccess==='function') window.onCaptchaSuccess(_t); })()""" % tok)
            self.page.wait_for_timeout(1200)
        except Exception:
            pass

    def _submit_login(self, username: str, password: str, token: str, lang: str) -> tuple[str, str]:
        js = """() => { const f=document.createElement('form');
            f.method='POST'; f.action=%s; f.enctype='application/x-www-form-urlencoded';
            const d={'username':%s,'password':%s,'language':%s,'rgpd':'Y','captchaResponse':%s};
            for (const k in d){ const i=document.createElement('input');
              i.type='hidden'; i.name=k; i.value=d[k]; f.appendChild(i);}
            document.body.appendChild(f); f.submit(); }""" % (
            json.dumps(LOGIN_URL), json.dumps(username), json.dumps(password),
            json.dumps(lang), json.dumps(token))
        try:
            with self.page.expect_navigation(wait_until="load", timeout=int(45000 * self.nav_scale)):
                self.page.evaluate(js)
        except Exception:
            pass
        content = ""
        for _ in range(6):
            try:
                self.page.wait_for_timeout(1500)
                content = self.page.content() or ""
                break
            except Exception:
                try:
                    self.page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
        return self.page.url, content

    def login(self, username: str, password: str, capsolver_keys: list[str], *,
              lang: str = "PT", min_score: int = 0, solve_proxy: bool = False) -> dict:
        """Full login. Returns {ok, final_url, token_len, body_len}. ok is the HAR-confirmed success
        signature: landed at /login with a small 200 body and no bd.js challenge."""
        if not self._nav_to_login_form():
            diag = self.page_diag()
            blocked = any(m in diag["markers"] for m in
                          ("handheldfriendly", "request could not be processed", "datadome"))
            reason = ("WAF/DataDome block on auth page (proxy blocked for login)"
                      if blocked else "login form never rendered")
            return {"ok": False, "final_url": self.page.url, "reason": reason,
                    "waf_blocked": blocked, "diag": diag}
        time.sleep(random.uniform(1.0, 2.0))
        self._type_credentials(username, password)

        proxy_info = solver.parse_proxy_for_capsolver(self.proxy) if solve_proxy else None
        token = ""
        try:
            token = solver.solve(capsolver_keys, action="LOGIN_EVISA",
                                 proxy_info=proxy_info, min_score=min_score)
        except solver.SolverError as e:
            return {"ok": False, "final_url": self.page.url, "reason": f"solver: {e}"}

        self._inject_token(token)
        dd_ok = self._settle_datadome()   # let bd.js set the datadome cookie before POSTing /login
        final_url, content = self._submit_login(username, password, token, lang)
        has_challenge = "/ch/bd.js" in content
        at_login = final_url.rstrip("/").endswith("/VistosOnline/login")
        ok = at_login and not has_challenge and len(content) < 500
        low = content.lower()
        result = {
            "ok": ok, "final_url": final_url, "token_len": len(token), "token_score": solver.score_token(token),
            "body_len": len(content), "challenge": has_challenge, "datadome_cookie": dd_ok,
        }
        if not ok:
            # Classify the post-submit body so we know WHY (WAF re-block vs reCAPTCHA reject vs creds).
            result["post_markers"] = [m for m in (
                "request could not be processed", "handheldfriendly", "unexpected_redirect",
                "datadome", "type=error", "type=success", "terminar", "invalid", "captcha",
            ) if m in low]
            result["post_snippet"] = re.sub(r"\s+", " ", content)[:400]
        return result
