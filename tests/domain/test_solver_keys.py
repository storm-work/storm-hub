"""Which solver key a solve uses: even spread, skip a key that is out of balance, fail over, shared state.
The vendored race_all is driven with fake provider functions -- no solver service, no network."""
import collections

import fakeredis
import pytest

import libs.engine.vendor  # noqa: F401  (vendored modules on sys.path)
import solver
from libs.domain import solver_keys
from libs.domain.solver_keys import KeyBook, is_key_failure
from libs.domain.solver_select import resolve_solver_keys, rotated_solver_lists

ZERO = "createTask error: {'errorId': 10, 'errorCode': 'ERROR_ZERO_BALANCE', 'errorDescription': 'Account has zero or negative balance'}"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


# ── the book ────────────────────────────────────────────────────────────────────────────────────────
def test_live_keys_are_used_in_even_rotation():
    book = KeyBook()
    firsts = collections.Counter(book.order("anticaptcha", ["a", "b", "c"])[0] for _ in range(30))
    assert firsts == {"a": 10, "b": 10, "c": 10}, "30 solves over 3 keys: 10 each, not 30 on key #1"


def test_the_callers_list_order_does_not_change_the_spread():
    """Registration/booking consumers rotate their key list per account; the book's counter must not be
    cancelled by that. Two consecutive solves from two differently-rotated lists must use DIFFERENT keys."""
    book = KeyBook()
    first = book.order("anticaptcha", ["a", "b"])[0]      # account 0's rotation
    second = book.order("anticaptcha", ["b", "a"])[0]     # account 1's rotation
    assert first != second
    firsts = collections.Counter(
        book.order("anticaptcha", ["a", "b", "c"][i % 3:] + ["a", "b", "c"][:i % 3])[0] for i in range(30))
    assert firsts == {"a": 10, "b": 10, "c": 10}


def test_a_single_key_or_none_is_returned_as_is():
    book = KeyBook()
    assert book.order("anticaptcha", ["only"]) == ["only"] and book.order("anticaptcha", []) == []


def test_an_empty_balance_marks_the_key_dead_and_it_is_skipped():
    clock = Clock()
    book = KeyBook(clock=clock)
    assert book.report_fail("anticaptcha", "a", ZERO) is True
    for _ in range(6):
        assert book.order("anticaptcha", ["a", "b"]) == ["b", "a"], "live key first, the dead one only as a last resort"


def test_a_dead_key_is_tried_again_after_its_timeout_so_a_topup_is_noticed():
    clock = Clock()
    book = KeyBook(clock=clock)
    book.report_fail("anticaptcha", "a", ZERO)
    clock.t += solver_keys.DEAD_TTL_BALANCE + 1
    assert book.dead_until("anticaptcha", "a") == 0.0
    firsts = {book.order("anticaptcha", ["a", "b"])[0] for _ in range(4)}
    assert firsts == {"a", "b"}, "after the timeout the key is back in the rotation"


def test_a_failure_that_is_not_the_keys_fault_never_marks_it_dead():
    book = KeyBook()
    assert book.report_fail("anticaptcha", "a", "Solver timeout after 180s") is False
    assert book.report_fail("anticaptcha", "a", "createTask error: {'errorCode': 'ERROR_CAPTCHA_UNSOLVABLE'}") is False
    assert book.dead_until("anticaptcha", "a") == 0.0


def test_when_every_key_is_dead_none_is_dropped_and_the_soonest_to_recover_leads():
    clock = Clock()
    book = KeyBook(clock=clock)
    book.report_fail("anticaptcha", "a", "ERROR_KEY_DOES_NOT_EXIST")      # long timeout
    book.report_fail("anticaptcha", "b", ZERO)                            # short timeout
    assert book.order("anticaptcha", ["a", "b"]) == ["b", "a"]


def test_solvers_do_not_share_dead_marks_or_rotation():
    book = KeyBook()
    book.report_fail("anticaptcha", "k", ZERO)
    assert book.dead_until("capsolver", "k") == 0.0


def test_state_is_shared_between_processes_through_redis():
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    a, b = KeyBook(r), KeyBook(r)
    a.report_fail("anticaptcha", "k1", ZERO)
    assert b.dead_until("anticaptcha", "k1") > 0, "a key found empty by one worker is skipped by every other"
    seen = [x.order("capsolver", ["x", "y"])[0] for x in (a, b, a, b)]
    assert seen == ["y", "x", "y", "x"] or seen == ["x", "y", "x", "y"], "one fleet-wide rotation"


def test_a_redis_outage_degrades_to_in_process_state_and_never_raises():
    class Down:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise ConnectionError("down")
            return boom
    book = KeyBook(Down())
    book.report_fail("anticaptcha", "a", ZERO)
    assert book.order("anticaptcha", ["a", "b"]) == ["b", "a"]
    book.report_ok("anticaptcha", "b")


def test_stats_report_tries_failures_and_dead_time_without_exposing_keys():
    clock = Clock()
    book = KeyBook(clock=clock)
    book.report_ok("anticaptcha", "secret-key-d6ee")
    book.report_fail("anticaptcha", "secret-key-f84b", ZERO)
    rows = {r["key"]: r for r in book.stats("anticaptcha", ["secret-key-d6ee", "secret-key-f84b"])}
    assert set(rows) == {"...d6ee", "...f84b"}
    assert rows["...d6ee"]["ok"] == 1 and rows["...d6ee"]["dead_for_s"] == 0
    assert rows["...f84b"]["fail"] == 1 and rows["...f84b"]["dead_for_s"] > 800


def test_is_key_failure_recognises_each_providers_wording():
    for text in (ZERO, "ERROR_KEY_DOES_NOT_EXIST", "{'errorCode': 'ERROR_ZERO_BALANCE'}", "ERROR_WRONG_USER_KEY",
                 "ERROR_KEY_DENIED_ACCESS"):
        assert is_key_failure(text), text
    for text in ("Solver timeout after 180s", "ERROR_CAPTCHA_UNSOLVABLE", "poll error", "", None):
        assert not is_key_failure(text), text


# ── race_all with the book installed ────────────────────────────────────────────────────────────────
@pytest.fixture
def fake_provider(monkeypatch):
    """Replace the anticaptcha provider call; returns the list of keys it was invoked with."""
    calls = []
    behaviour = {}

    def fake(key, action, proxy_info, cookies):
        calls.append(key)
        err = behaviour.get(key)
        if err:
            raise RuntimeError(err)
        return "T" * 2100

    monkeypatch.setattr(solver, "_anticaptcha", fake)
    monkeypatch.setattr(solver, "KEY_BOOK", None)
    return calls, behaviour


def test_without_a_book_race_all_still_uses_only_the_first_key(fake_provider):
    calls, _ = fake_provider
    for _ in range(4):
        solver.race_all([], ["a", "b"], [], [], "LOGIN_EVISA")
    assert set(calls) == {"a"}, "the original behaviour, unchanged for any process that never installs a book"


def test_with_a_book_solves_are_spread_evenly_across_the_keys(fake_provider, monkeypatch):
    calls, _ = fake_provider
    monkeypatch.setattr(solver, "KEY_BOOK", KeyBook())
    for _ in range(10):
        solver.race_all([], ["a", "b"], [], [], "LOGIN_EVISA")
    assert collections.Counter(calls) == {"a": 5, "b": 5}


def test_an_empty_key_is_skipped_and_the_solve_still_succeeds_on_the_next(fake_provider, monkeypatch):
    calls, behaviour = fake_provider
    behaviour["a"] = ZERO
    monkeypatch.setattr(solver, "KEY_BOOK", KeyBook())
    for _ in range(8):
        assert solver.race_all([], ["a", "b"], [], [], "LOGIN_EVISA"), "every solve must still produce a token"
    assert calls.count("a") == 1, "the empty key is tried once, marked dead, and never wasted on again"
    assert calls.count("b") == 8


def test_a_failure_that_is_not_the_keys_does_not_hop_to_another_key(fake_provider, monkeypatch):
    calls, behaviour = fake_provider
    behaviour["a"] = "Solver timeout after 180s"
    monkeypatch.setattr(solver, "KEY_BOOK", KeyBook())
    book = solver.KEY_BOOK
    monkeypatch.setattr(book, "order", lambda s, keys: list(keys))        # force a first
    with pytest.raises(RuntimeError):
        solver.race_all([], ["a", "b"], [], [], "LOGIN_EVISA")
    assert calls == ["a"], "a timeout says nothing about key a -- retrying elsewhere is the caller's business"


def test_install_puts_a_book_behind_race_all():
    solver.KEY_BOOK = None
    book = solver_keys.install(None)
    assert solver.KEY_BOOK is book
    solver.KEY_BOOK = None


# ── what consumers hand race_all ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("selection", ["capsolver", "anticaptcha", "race"])
@pytest.mark.parametrize("index", [0, 1, 2, 7, 12345])
def test_rotated_lists_lead_with_the_key_the_old_single_pick_chose(selection, index):
    caps, acs = ["c1", "c2", "c3"], ["a1", "a2"]
    old_cs, old_ac = resolve_solver_keys(selection, caps, acs, index=index)
    cs, ac = rotated_solver_lists(selection, caps, acs, index=index)
    assert cs[:1] == old_cs and ac[:1] == old_ac, "list[0] must still be the per-consumer pick"
    assert sorted(cs) == sorted(caps if old_cs else []) and sorted(ac) == sorted(acs if old_ac else []), \
        "and the lists must carry EVERY key, so failover has somewhere to go"


def test_rotated_lists_fall_back_to_capsolver_when_there_is_no_anticaptcha_key():
    assert rotated_solver_lists("anticaptcha", ["c1", "c2"], [], index=1) == (["c2", "c1"], [])
