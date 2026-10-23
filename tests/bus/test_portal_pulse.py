"""PortalPulse -- the real-time, fleet-wide 'is the portal answering right now?' signal that riding
the portal's 30-90 s up-windows depends on (run 1023, 2026-10-23)."""
import threading
import time

import fakeredis
import pytest

from libs.bus.portal_pulse import PortalPulse, Vantage, classify_probe, probe_once


@pytest.fixture()
def r():
    return fakeredis.FakeStrictRedis(decode_responses=True)


# ── classification of an anonymous home-page probe ───────────────────────────────────────────
def test_the_application_answering_is_up():
    assert classify_probe(200, "<html>...VistosOnline login page...</html>") == "up"


def test_the_edge_refusing_a_datacenter_ip_is_refused_not_up():
    """MEASURED 2026-10-23 from the Hub server: 403 in 0.8 s with the 'try again later' page. The
    edge is reachable; that says nothing about the application."""
    body = ("Não foi possível processar o pedido / Request could not be processed ... "
            "We were unable to complete your request at this time. URI: pedidodevistos.mne.gov.pt/VistosOnline/")
    assert classify_probe(403, body) == "refused"


def test_maintenance_and_5xx_are_degraded():
    assert classify_probe(200, "<h1>Serviço Indisponível</h1>") == "degraded"
    assert classify_probe(503, "Service Unavailable") == "degraded"


def test_a_bot_gate_is_refused():
    assert classify_probe(200, '<script src="/ch/bd.js"></script>') == "refused"


# ── state machine ─────────────────────────────────────────────────────────────────────────────
def test_unknown_with_no_observations(r):
    assert PortalPulse(r).state()["state"] == "unknown"


def test_up(r):
    p = PortalPulse(r)
    p.observe("up", ms=812, src="w1")
    s = p.state()
    assert s["state"] == "up" and s["last_up_ms"] == 812 and s["last_up_src"] == "w1"


def test_mostly_failing_with_some_answers_is_flapping(r):
    """The stress cycle: the portal answers now and then. Hammer -- fire on each up."""
    p = PortalPulse(r)
    p.observe("up")
    for _ in range(6):
        p.observe("noanswer")
    assert p.state()["state"] == "flapping"


def test_no_answers_is_down_and_errors_only_is_degraded(r):
    p = PortalPulse(r)
    p.observe("noanswer")
    assert p.state()["state"] == "down"
    p.observe("degraded")
    assert p.state()["state"] == "degraded"


def test_edge_refusals_alone_are_reachable_not_up(r):
    p = PortalPulse(r)
    p.observe("refused")
    assert p.state()["state"] == "reachable"


def test_observations_age_out_of_the_window(r):
    p = PortalPulse(r, window=0.2)
    p.observe("up")
    time.sleep(0.3)
    assert p.state()["state"] == "unknown"


def test_transitions_are_logged_as_measured_windows(r):
    p = PortalPulse(r, window=0.2)
    p.observe("noanswer"); assert p.tick() == "down"
    assert p.tick() is None, "no change, no log entry"
    time.sleep(0.25)
    p.observe("up"); assert p.tick() == "up"
    time.sleep(0.25)
    p.observe("noanswer"); assert p.tick() == "down"
    w = p.windows()
    assert [x["state"] for x in w] == ["up", "down"]          # newest first, closed intervals only
    assert w[0]["seconds"] >= 0.2


def test_wait_for_up_wakes_on_any_answer_after_the_wait_began(r):
    p = PortalPulse(r)
    t0 = time.time()
    threading.Timer(0.3, lambda: p.observe("up")).start()
    assert p.wait_for_up(t0, timeout=3, poll=0.05) is True
    assert time.time() - t0 < 1.5


def test_wait_for_up_ignores_an_answer_from_before_the_wait(r):
    p = PortalPulse(r)
    p.observe("up")
    assert p.wait_for_up(time.time() + 0.01, timeout=0.3, poll=0.05) is False


def test_observe_never_raises():
    class _Broken:
        def pipeline(self): raise RuntimeError("redis down")
    PortalPulse(_Broken()).observe("up")      # must not raise


# ── the prober ────────────────────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status, text): self.status_code, self.text = status, text


def _fake_client(monkeypatch, portal, echo="1.2.3.4"):
    import libs.bus.portal_pulse as mod

    class _C:
        def get(self, url, **kw):
            if "ipify" in url:
                if isinstance(echo, Exception):
                    raise echo
                return _Resp(200, echo)
            if isinstance(portal, Exception):
                raise portal
            return portal
    monkeypatch.setattr(mod, "_client", lambda *a, **k: _C())


def test_probe_records_up(r, monkeypatch):
    _fake_client(monkeypatch, _Resp(200, "VistosOnline home"))
    p = PortalPulse(r)
    assert probe_once(Vantage("soax1", lambda: "http://x:1", proxy="http://x:1"), p) == "up"
    assert p.state()["state"] == "up"


def test_no_answer_from_a_live_exit_is_noanswer(r, monkeypatch):
    _fake_client(monkeypatch, TimeoutError("operation timed out"), echo="1.2.3.4")
    p = PortalPulse(r)
    assert probe_once(Vantage("soax1", lambda: "http://x:1", proxy="http://x:1"), p) == "noanswer"
    assert p.state()["state"] == "down"


def test_a_dead_exit_is_not_evidence_and_is_re_minted(r, monkeypatch):
    _fake_client(monkeypatch, TimeoutError("operation timed out"), echo=ConnectionError("tunnel"))
    p = PortalPulse(r)
    minted = []
    v = Vantage("soax1", lambda: (minted.append(1), "http://new:1")[1], proxy="http://old:1")
    assert probe_once(v, p) is None
    assert p.state()["state"] == "unknown", "a dead exit must say nothing about the portal"
    assert v.proxy == "http://new:1" and minted
