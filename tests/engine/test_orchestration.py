from libs.engine import orchestration as orch

MIN = 30.0
HANG = 180.0


def _plan(state, healthy, now):
    return orch.plan_rotation_round(state, healthy, now=now, min_interval=MIN, hang_timeout=HANG)


def test_rotation_picks_one_scout_at_a_time_round_robin():
    """One round in flight at a time; successive REAL rounds cycle through the healthy scouts in
    order, each spaced MIN apart."""
    state = orch.ScoutPoolState()
    scouts = ["s1", "s2", "s3"]
    # first dispatch at t=0
    assert _plan(state, scouts, now=0.0) == "s1"
    # while s1 is in flight, nothing else may dispatch
    assert _plan(state, scouts, now=1.0) is None
    # s1 reports a real round -> next dispatch must wait MIN
    orch.on_round_reported(state, "s1", [{"date": "d1", "outcome": "empty"}], now=2.0)
    assert _plan(state, scouts, now=2.0 + MIN - 1) is None       # still inside the interval
    assert _plan(state, scouts, now=2.0 + MIN) == "s2"           # next scout in rotation
    orch.on_round_reported(state, "s2", [{"date": "d1", "outcome": "found"}], now=40.0)
    assert _plan(state, scouts, now=40.0 + MIN) == "s3"
    orch.on_round_reported(state, "s3", [{"date": "d1", "outcome": "empty"}], now=80.0)
    assert _plan(state, scouts, now=80.0 + MIN) == "s1"          # wraps around


def test_failed_round_advances_immediately_without_pacing():
    """A failed round (all-'failed', or an empty report) is not a real attempt: the rotation moves to
    the next scout immediately, skipping the MIN pacing gate."""
    state = orch.ScoutPoolState()
    scouts = ["s1", "s2", "s3"]
    assert _plan(state, scouts, now=0.0) == "s1"
    orch.on_round_reported(state, "s1", [{"date": "d1", "outcome": "failed"}], now=1.0)  # FAILED
    # no MIN wait -- next scout may go right now
    assert _plan(state, scouts, now=1.0) == "s2"
    orch.on_round_reported(state, "s2", [], now=2.0)                                     # empty report = fail
    assert _plan(state, scouts, now=2.0) == "s3"


def test_real_round_after_a_failure_restores_pacing():
    state = orch.ScoutPoolState()
    scouts = ["s1", "s2"]
    _plan(state, scouts, now=0.0)                                   # -> s1
    orch.on_round_reported(state, "s1", [{"date": "d", "outcome": "failed"}], now=1.0)
    assert _plan(state, scouts, now=1.0) == "s2"                    # failure -> immediate
    orch.on_round_reported(state, "s2", [{"date": "d", "outcome": "empty"}], now=2.0)  # REAL
    assert _plan(state, scouts, now=2.0 + MIN - 1) is None          # paced again
    assert _plan(state, scouts, now=2.0 + MIN) == "s1"


def test_no_healthy_scouts_yields_nothing():
    state = orch.ScoutPoolState()
    assert _plan(state, [], now=0.0) is None
    # an in-flight scout that drops out of the healthy set is reclaimed, but with nobody to hand to
    # the round still yields nothing this tick.
    _plan(state, ["s1"], now=0.0)
    assert _plan(state, [], now=1.0) is None


def test_hung_round_is_reclaimed_after_the_hang_timeout():
    """A round that never reports must not wedge the single in-flight slot forever."""
    state = orch.ScoutPoolState()
    scouts = ["s1", "s2"]
    assert _plan(state, scouts, now=0.0) == "s1"
    assert _plan(state, scouts, now=HANG - 1) is None              # still within timeout
    # past the timeout: s1 is reclaimed as failed, rotation advances immediately to s2
    assert _plan(state, scouts, now=HANG) == "s2"


def test_vanished_inflight_scout_is_reclaimed():
    """A scout that dies/reaps while in flight (drops out of healthy_ids) frees the slot at once."""
    state = orch.ScoutPoolState()
    assert _plan(state, ["s1", "s2"], now=0.0) == "s1"
    # s1 gone from the healthy set -> reclaimed as failed, s2 dispatched with no pacing wait
    assert _plan(state, ["s2"], now=1.0) == "s2"


def test_on_round_reported_only_frees_the_matching_scout():
    state = orch.ScoutPoolState()
    _plan(state, ["s1", "s2"], now=0.0)          # inflight = s1
    orch.on_round_reported(state, "s2", [{"date": "d", "outcome": "found"}], now=1.0)  # stray report
    assert state.inflight == "s1"                # s1 still in flight
    orch.on_round_reported(state, "s1", [{"date": "d", "outcome": "found"}], now=1.0)
    assert state.inflight is None


def test_round_reached_portal_classification():
    assert orch._round_reached_portal([{"date": "d", "outcome": "empty"}]) is True    # portal said 'none'
    assert orch._round_reached_portal([{"date": "d", "outcome": "found"}]) is True
    assert orch._round_reached_portal([{"date": "d", "outcome": "failed"}]) is False  # never reached
    assert orch._round_reached_portal([]) is False                                    # nothing reported


def test_should_send_assign_dedups_outstanding():
    outstanding = {"w1"}
    assert orch.should_send_assign("w1", outstanding) is False
    assert orch.should_send_assign("w2", outstanding) is True
