"""
mail.tm temporary email API wrapper + Cloudflare Email Routing worker client.
Vendored verbatim from storm-auto's app/core/email_pool.py (2026-08-13) for the registration
subsystem port -- same "proven code, don't rewrite" treatment as session.py/solver.py. Only change
from the original: load_cf_mail_from_env() (which read a local .env file) is dropped since
storm-hub passes CF_MAIL_DOMAIN/CF_WORKER_URL/CF_WORKER_SECRET through the launch payload from the
Hub's global settings store instead -- construct CloudflareMail(...) directly with those values.

No API key required for MailTM. Creates disposable inboxes for account registration.

Usage:
    from email_pool import MailTM

    m = MailTM()
    address, password = m.create()          # e.g. ("abc123@finmail.com", "pw")
    jwt = m.token(address, password)
    code = m.poll_for_code(jwt, timeout=120)  # blocks until code arrives
    if code:
        print("verification code:", code)

API docs: https://api.mail.tm
"""

import asyncio
import re
import time
import string
import random
import requests

_BASE  = "https://api.mail.tm"
_SESS  = requests.Session()
_SESS.headers.update({"Content-Type": "application/json"})

# Regex to match a UUID with dashes (the code token typed into the form)
_UUID_RE = re.compile(
    r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
    re.IGNORECASE,
)
# Regex to match the link token from ?token=<32-hex> URL
_LINK_TOKEN_RE = re.compile(r'[?&]token=([0-9a-f]{32})\b', re.IGNORECASE)


def _random_str(n: int) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


class MailTM:
    def __init__(self, timeout: int = 10):
        self._timeout = timeout
        self._domain  = None

    def domain(self) -> str:
        if self._domain:
            return self._domain
        r = _SESS.get(f"{_BASE}/domains", timeout=self._timeout)
        r.raise_for_status()
        members = r.json().get("hydra:member", [])
        if not members:
            raise RuntimeError("mail.tm: no domains available")
        self._domain = members[0]["domain"]
        return self._domain

    def create(self, address: str | None = None,
               password: str | None = None) -> tuple[str, str]:
        """
        Create a new mailbox. Returns (address, password).
        If address is not given, a random one is generated.
        """
        dom = self.domain()
        if not address:
            address = f"{_random_str(10)}@{dom}"
        if not password:
            password = _random_str(16)

        r = _SESS.post(f"{_BASE}/accounts",
                       json={"address": address, "password": password},
                       timeout=self._timeout)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"mail.tm create failed ({r.status_code}): {r.text[:200]}")
        print(f"[email] created: {address}")
        return address, password

    def token(self, address: str, password: str) -> str:
        """Get a JWT to access this mailbox."""
        r = _SESS.post(f"{_BASE}/token",
                       json={"address": address, "password": password},
                       timeout=self._timeout)
        r.raise_for_status()
        return r.json()["token"]

    def messages(self, jwt: str) -> list[dict]:
        """Return list of message summaries."""
        r = _SESS.get(f"{_BASE}/messages",
                      headers={"Authorization": f"Bearer {jwt}"},
                      timeout=self._timeout)
        r.raise_for_status()
        return r.json().get("hydra:member", [])

    def message_body(self, jwt: str, msg_id: str) -> str:
        """Return full text/html body of a message."""
        r = _SESS.get(f"{_BASE}/messages/{msg_id}",
                      headers={"Authorization": f"Bearer {jwt}"},
                      timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        # Prefer HTML body, fall back to text
        return data.get("html", "") or data.get("text", "") or str(data)

    def extract_tokens(self, body: str) -> dict:
        """
        Extract both tokens from an E-VISA verification email.

        The server sends two different tokens:
          - link_token: 32-char hex in the ?token= URL (used as tokenSearchParams)
          - code_token: standalone UUID with dashes in the body text (typed into tokenInput)

        Returns {"link": link_token_or_None, "code": code_token_or_None}
        """
        if isinstance(body, list):
            body = " ".join(str(b) for b in body)
        body = str(body)

        link_match = _LINK_TOKEN_RE.search(body)
        link_token = link_match.group(1).lower() if link_match else None

        body_stripped = re.sub(r'https?://\S+', ' ', body)
        code_match = _UUID_RE.search(body_stripped)
        code_token = code_match.group(1).lower() if code_match else None

        return {"link": link_token, "code": code_token}

    def extract_code(self, body: str) -> str | None:
        """Legacy: returns the link token hex (kept for backwards compat)."""
        tokens = self.extract_tokens(body)
        return tokens.get("link") or tokens.get("code")

    def poll_for_tokens(self, jwt: str, timeout: int = 180,
                        interval: int = 5) -> dict | None:
        """
        Poll inbox until a verification email arrives.
        Returns {"link": <hex>, "code": <uuid-with-dashes>} or None on timeout.
        """
        deadline = time.time() + timeout
        seen_ids: set[str] = set()

        try:
            for m in self.messages(jwt):
                seen_ids.add(m["id"])
        except Exception:
            pass

        print(f"[email] polling inbox (timeout={timeout}s)...")
        while time.time() < deadline:
            time.sleep(interval)
            try:
                msgs = self.messages(jwt)
            except Exception as e:
                print(f"[email] poll error: {e}")
                continue

            new_msgs = [m for m in msgs if m["id"] not in seen_ids]
            for msg in new_msgs:
                seen_ids.add(msg["id"])
                print(f"[email] new message from {msg.get('from', {}).get('address','?')}: "
                      f"{msg.get('subject','(no subject)')}")
                try:
                    body = self.message_body(jwt, msg["id"])
                    tokens = self.extract_tokens(body)
                    if tokens["link"] or tokens["code"]:
                        print(f"[email] link_token={tokens['link']}  code_token={tokens['code']}")
                        return tokens
                    else:
                        print(f"[email] no tokens found in message body (len={len(str(body))})")
                except Exception as e:
                    print(f"[email] body fetch error: {e}")

        print(f"[email] poll timeout after {timeout}s — no tokens received")
        return None

    def poll_for_code(self, jwt: str, timeout: int = 180,
                      interval: int = 5) -> str | None:
        """Legacy: returns link token hex. Use poll_for_tokens() for full token pair."""
        tokens = self.poll_for_tokens(jwt, timeout=timeout, interval=interval)
        if tokens is None:
            return None
        return tokens.get("link") or tokens.get("code")

    async def async_poll_for_tokens(self, jwt: str, timeout: int = 180,
                                    interval: int = 5) -> dict | None:
        """
        Async version of poll_for_tokens. Uses asyncio.sleep so 100s of concurrent
        coroutines can all wait for emails without blocking the event loop.
        Returns {"link": <hex>, "code": <uuid-with-dashes>} or None on timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        seen_ids: set[str] = set()

        try:
            for m in self.messages(jwt):
                seen_ids.add(m["id"])
        except Exception:
            pass

        print(f"[email] async polling inbox (timeout={timeout}s)...")
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(interval)
            try:
                msgs = self.messages(jwt)
            except Exception as e:
                print(f"[email] poll error: {e}")
                continue

            new_msgs = [m for m in msgs if m["id"] not in seen_ids]
            for msg in new_msgs:
                seen_ids.add(msg["id"])
                print(f"[email] new message: {msg.get('subject', '(no subject)')}")
                try:
                    body = self.message_body(jwt, msg["id"])
                    tokens = self.extract_tokens(body)
                    if tokens["link"] or tokens["code"]:
                        print(f"[email] link_token={tokens['link']}  code_token={tokens['code']}")
                        return tokens
                except Exception as e:
                    print(f"[email] body fetch error: {e}")

        print(f"[email] async poll timeout after {timeout}s — no tokens received")
        return None


# Module-level singleton
_pool: MailTM | None = None

def get_pool() -> MailTM:
    global _pool
    if _pool is None:
        _pool = MailTM()
    return _pool


# ── Cloudflare email routing ───────────────────────────────────────────────────

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class CloudflareMail:
    """
    Email inbox via Cloudflare Email Routing + KV Worker.

    Worker API:
      GET  {WORKER_URL}?email={full_email}   x-secret: {SECRET}
        → {found: bool, linkToken: str, codeToken: str}
      DELETE {WORKER_URL}?email={full_email} x-secret: {SECRET}
        → clears inbox
    """

    def __init__(self,
                 domain: str | None = None,
                 worker_url: str | None = None,
                 secret: str | None = None,
                 timeout: int = 10):
        self._domain     = domain     or "maillab.live"
        self._worker_url = (worker_url or "https://octo_mail_keeper.alexneborachkoupwork.workers.dev").rstrip("/")
        self._secret     = secret     or "octo-mail-keeper-secret"
        self._timeout    = timeout

    def create(self, local: str | None = None) -> tuple[str, str]:
        """
        Generate an inbox address.
        Returns (email_address, local_part).
        The local_part doubles as the polling key.
        """
        local = local or _random_str(12)
        email = f"{local}@{self._domain}"
        print(f"[cf_email] inbox: {email}")
        return email, local

    def poll_for_tokens(self, email_or_local: str, timeout: int = 180,
                        interval: int = 5) -> dict | None:
        """
        Poll CF Worker until email arrives with tokens.
        Returns {"link": hex32, "code": uuid-with-dashes} or None on timeout.
        """
        email = (email_or_local if "@" in email_or_local
                 else f"{email_or_local}@{self._domain}")
        deadline = time.time() + timeout

        print(f"[cf_email] polling {email} (timeout={timeout}s)...")
        while time.time() < deadline:
            time.sleep(interval)
            try:
                r = requests.get(
                    self._worker_url,
                    params={"email": email},
                    headers={"x-secret": self._secret},
                    timeout=self._timeout,
                    verify=False,
                )
                if r.status_code == 401:
                    print("[cf_email] worker: unauthorized (wrong secret?)")
                    return None
                if not r.ok:
                    print(f"[cf_email] worker HTTP {r.status_code}")
                    continue

                data = r.json()

                if isinstance(data, dict):
                    if data.get("found"):
                        tokens = {
                            "link": data.get("linkToken") or data.get("link") or "",
                            "code": data.get("codeToken") or data.get("code") or "",
                        }
                        print(f"[cf_email] tokens found: link={tokens['link']}  code={tokens['code']}")
                        return tokens
                    continue

                if isinstance(data, list) and data:
                    for msg in reversed(data):
                        body = msg.get("text", "")
                        tokens = _extract_tokens_from_body(body)
                        if tokens["link"] or tokens["code"]:
                            print(f"[cf_email] tokens found: link={tokens['link']}  code={tokens['code']}")
                            return tokens

            except Exception as e:
                print(f"[cf_email] poll error: {e}")

        print(f"[cf_email] poll timeout after {timeout}s")
        return None

    async def async_poll_for_tokens(self, email_or_local: str, timeout: int = 180,
                                    interval: int = 5) -> dict | None:
        """Async version — uses asyncio.sleep so concurrent coroutines don't block."""
        email = (email_or_local if "@" in email_or_local
                 else f"{email_or_local}@{self._domain}")
        deadline = asyncio.get_event_loop().time() + timeout

        print(f"[cf_email] async polling {email} (timeout={timeout}s)...")
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(interval)
            try:
                r = requests.get(
                    self._worker_url,
                    params={"email": email},
                    headers={"x-secret": self._secret},
                    timeout=self._timeout,
                    verify=False,
                )
                if not r.ok:
                    continue
                data = r.json()

                if isinstance(data, dict) and data.get("found"):
                    tokens = {
                        "link": data.get("linkToken") or data.get("link") or "",
                        "code": data.get("codeToken") or data.get("code") or "",
                    }
                    print(f"[cf_email] tokens found: link={tokens['link']}  code={tokens['code']}")
                    return tokens

                if isinstance(data, list) and data:
                    for msg in reversed(data):
                        tokens = _extract_tokens_from_body(msg.get("text", ""))
                        if tokens["link"] or tokens["code"]:
                            print(f"[cf_email] tokens found: link={tokens['link']}  code={tokens['code']}")
                            return tokens
            except Exception as e:
                print(f"[cf_email] async poll error: {e}")

        print(f"[cf_email] async poll timeout after {timeout}s")
        return None

    def delete(self, email_or_local: str) -> None:
        """Clean up the inbox after use."""
        email = (email_or_local if "@" in email_or_local
                 else f"{email_or_local}@{self._domain}")
        try:
            requests.delete(
                self._worker_url,
                params={"email": email},
                headers={"x-secret": self._secret},
                timeout=self._timeout,
            )
        except Exception:
            pass


def _extract_tokens_from_body(body: str) -> dict:
    """Extract link+code tokens from raw email body text."""
    body = str(body)
    link_match = _LINK_TOKEN_RE.search(body)
    link_token = link_match.group(1).lower() if link_match else None
    body_stripped = re.sub(r'https?://\S+', ' ', body)
    code_match = _UUID_RE.search(body_stripped)
    code_token = code_match.group(1).lower() if code_match else None
    return {"link": link_token, "code": code_token}
