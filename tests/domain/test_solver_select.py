"""libs.domain.solver_select -- solver selection + per-consumer key distribution."""
from libs.domain.solver_select import (active_solver_lists, parse_keys,
                                       resolve_solver_keys, resolve_selection, rr_index)

CS = ["cap1", "cap2"]
AC = ["ac1", "ac2", "ac3"]


def test_parse_keys_drops_blanks():
    assert parse_keys(" a , b ,, c ") == ["a", "b", "c"]
    assert parse_keys("") == [] and parse_keys(None) == []


def test_capsolver_picks_one_key_distributed_by_index():
    assert resolve_solver_keys("capsolver", CS, AC, index=0) == (["cap1"], [])
    assert resolve_solver_keys("capsolver", CS, AC, index=1) == (["cap2"], [])
    assert resolve_solver_keys("capsolver", CS, AC, index=2) == (["cap1"], [])  # wraps


def test_anticaptcha_picks_one_and_falls_back_to_capsolver_when_unset():
    assert resolve_solver_keys("anticaptcha", CS, AC, index=0) == ([], ["ac1"])
    assert resolve_solver_keys("anticaptcha", CS, AC, index=4) == ([], ["ac2"])  # 4 % 3
    # misconfig: anticaptcha selected but no anticaptcha keys -> fall back to a capsolver key
    assert resolve_solver_keys("anticaptcha", CS, [], index=0) == (["cap1"], [])


def test_race_picks_one_of_each():
    assert resolve_solver_keys("race", CS, AC, index=1) == (["cap2"], ["ac2"])
    # race with one provider empty -> just the non-empty one (lets active_solver_lists pre-filter)
    assert resolve_solver_keys("race", CS, [], index=0) == (["cap1"], [])


def test_unknown_solver_defaults_to_capsolver():
    assert resolve_solver_keys("nope", CS, AC, index=0) == (["cap1"], [])


def test_active_solver_lists_returns_full_lists_per_selection():
    assert active_solver_lists("capsolver", CS, AC) == (CS, [])
    assert active_solver_lists("anticaptcha", CS, AC) == ([], AC)
    assert active_solver_lists("anticaptcha", CS, []) == (CS, [])   # fallback
    assert active_solver_lists("race", CS, AC) == (CS, AC)


def test_resolve_selection_inherits_or_overrides():
    """Per-action LOGIN_/SCHEDULE_RECAPTCHA_SOLVER: 'inherit'/blank -> the base RECAPTCHA_SOLVER; an
    explicit solver overrides it -- so login and the schedule page can use different solvers."""
    assert resolve_selection("inherit", "anticaptcha") == "anticaptcha"
    assert resolve_selection("", "race") == "race"
    assert resolve_selection(None, "capsolver") == "capsolver"
    assert resolve_selection("capsolver", "anticaptcha") == "capsolver"   # explicit overrides base
    assert resolve_selection("ANTICAPTCHA", "capsolver") == "anticaptcha"  # case/space-normalised
    assert resolve_selection("inherit", None) == "capsolver"               # base unset -> capsolver


def test_login_and_schedule_can_resolve_to_different_solvers():
    """The operator ask: login via anticaptcha, schedule page via capsolver, same key pools."""
    base = "race"
    login = resolve_solver_keys(resolve_selection("anticaptcha", base), CS, AC, index=0)
    sched = resolve_solver_keys(resolve_selection("capsolver", base), CS, AC, index=0)
    assert login == ([], ["ac1"])        # login -> anticaptcha
    assert sched == (["cap1"], [])        # schedule -> capsolver


def test_rr_index_is_stable_and_nonnegative():
    assert rr_index("w1") == rr_index("w1") >= 0
    # distributes: across many ids, both halves of a 2-key list are hit
    mods = {rr_index(f"w{i}") % 2 for i in range(50)}
    assert mods == {0, 1}
