

# ── shutdown must not be blockable by an open dashboard (2026-08-21) ─────────
def test_close_subscribers_wakes_every_open_reader():
    """ws_events is `while True: await q.get()`, which never returns on its own. Uvicorn will not
    finish shutting down while a socket is still being served, so an open dashboard tab was enough
    to hang the Hub on "Waiting for background tasks to complete" until systemd's stop timeout
    expired -- leaving the unit `failed` and, because the scheduler never ticked again, a worker row
    stranded in RUNNING with no process behind it. Observed live 2026-08-21 03:42.

    The F16 hazard the plan names directly: never let an event-loop pattern include a
    forever-blocking coroutine with no exit path."""
    import asyncio

    from apps.hub.events import EventHub

    hub = EventHub.__new__(EventHub)
    hub._subs = set()
    a, b = hub.subscribe(), hub.subscribe()

    hub.close_subscribers()

    assert a.get_nowait() is EventHub.CLOSE
    assert b.get_nowait() is EventHub.CLOSE
    assert hub._subs == set(), "subscribers must be dropped, not left to be woken twice"


def test_close_subscribers_is_safe_with_none_open():
    from apps.hub.events import EventHub
    hub = EventHub.__new__(EventHub)
    hub._subs = set()
    hub.close_subscribers()          # must not raise
    assert hub._subs == set()


def test_a_full_subscriber_queue_does_not_block_shutdown():
    """A dashboard that stopped reading must not be able to hold the Hub open."""
    import asyncio

    from apps.hub.events import EventHub

    hub = EventHub.__new__(EventHub)
    hub._subs = set()
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait({"filler": True})
    hub._subs.add(q)

    hub.close_subscribers()          # QueueFull is swallowed deliberately
    assert hub._subs == set()
