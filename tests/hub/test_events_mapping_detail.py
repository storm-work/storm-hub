"""A substate change always rewrites the detail (2026-10-26).

The detail used to be written only when non-empty, so a worker that recovered to KEEPING with an
empty detail kept its old "not bookable: ..." text -- and _assign_slots excludes KEEPING workers
carrying that prefix. 29 such episodes on 2026-10-23..26."""
from apps.hub.events import map_event_to_update


def test_a_substate_change_with_no_detail_clears_the_old_one():
    assert map_event_to_update("status_change", {"substate": "keeping", "detail": ""}) == \
        {"substate": "keeping", "substate_detail": None}


def test_a_substate_change_with_a_detail_writes_it():
    upd = map_event_to_update("status_change", {"substate": "logging_in",
                                                "detail": "not bookable: re-authenticating"})
    assert upd == {"substate": "logging_in", "substate_detail": "not bookable: re-authenticating"}


def test_a_detail_only_update_still_works():
    assert map_event_to_update("status_change", {"detail": "x"}) == {"substate_detail": "x"}
