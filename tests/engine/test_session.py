import pytest

from libs.engine.session import BrowserSession, _split_proxy


def test_split_proxy_forms():
    assert _split_proxy("http://user:pass@1.2.3.4:5000") == {
        "server": "http://1.2.3.4:5000", "username": "user", "password": "pass"}
    assert _split_proxy("http://proxy.soax.com:5000") == {"server": "http://proxy.soax.com:5000"}
    assert _split_proxy(None) is None
    assert _split_proxy("garbage") is None


def test_browser_launches_and_evals():
    """Real Chromium smoke test (shared build 1228). Skips if the browser can't launch."""
    try:
        s = BrowserSession(headless=True).start()
    except Exception as e:  # pragma: no cover - environment without chromium
        pytest.skip(f"chromium unavailable: {e}")
    try:
        s.page.set_content("<input name='username' value='x'><div id='t'>hello</div>")
        assert s.eval("document.querySelector('input[name=username]') ? 1 : 0") == 1
        assert s.eval("document.getElementById('t').textContent") == "hello"
    finally:
        s.close()
