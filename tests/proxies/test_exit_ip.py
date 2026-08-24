"""Observing our own exit address -- the precondition for telling "our IP moved" apart from "the
portal refused us". See libs/proxies/exit_ip.py for why that distinction cost us days."""
from libs.proxies.exit_ip import exit_changed


def test_a_change_is_only_a_change_when_both_are_known():
    assert exit_changed("1.2.3.4", "5.6.7.8") is True
    assert exit_changed("1.2.3.4", "1.2.3.4") is False


def test_an_unknown_reading_is_never_a_change():
    """Biased to False deliberately: a false positive costs a full session rebuild -- a fresh login
    and a CAPTCHA solve out of the budget that is already the binding constraint -- so a flaky echo
    service must never be able to trigger one."""
    assert exit_changed(None, "5.6.7.8") is False
    assert exit_changed("1.2.3.4", None) is False
    assert exit_changed(None, None) is False
    assert exit_changed("", "5.6.7.8") is False


def test_observe_returns_none_rather_than_raising_when_unreachable():
    """It runs on every keepalive; it must never be able to kill a worker."""
    from libs.proxies.exit_ip import observe_exit_ip

    assert observe_exit_ip("http://user:pass@127.0.0.1:9", timeout=2) is None


def test_a_html_error_page_is_not_mistaken_for_an_address(monkeypatch):
    import libs.proxies.exit_ip as mod

    class _C:
        def __init__(self, **kw): pass
        def get(self, url):
            class _R: text = "<html><body>502 Bad Gateway</body></html>"
            return _R()

    import types
    monkeypatch.setitem(__import__("sys").modules, "primp", types.SimpleNamespace(Client=_C))
    assert mod.observe_exit_ip("http://x:1") is None
