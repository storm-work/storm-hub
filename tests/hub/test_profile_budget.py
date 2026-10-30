import asyncio

from apps.registrar import profile_budget as pb


class _FakeRedis:
    def __init__(self):
        self.z = {}

    def zremrangebyscore(self, key, lo, hi):
        self.z = {m: s for m, s in self.z.items() if not (lo <= s <= hi)}

    def zcard(self, key):
        return len(self.z)

    def zadd(self, key, mapping):
        self.z.update(mapping)

    def zrange(self, key, a, b, withscores=False):
        return sorted(self.z.items(), key=lambda kv: kv[1])[:1]


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_takes_slots_until_the_cap_then_waits_for_the_oldest_to_age_out():
    r, now, naps = _FakeRedis(), [1000.0], []

    async def sleep(s):
        naps.append(s)
        now[0] += s

    async def go():
        for _ in range(3):
            assert await pb.reserve(r, 3, clock=lambda: now[0], sleep=sleep, log=lambda *_: None) == 0.0
        return await pb.reserve(r, 3, window=100.0, clock=lambda: now[0], sleep=sleep, log=lambda *_: None)

    waited = _run(go())
    assert waited > 0 and naps          # the 4th had to wait
    assert len(r.z) <= 3                # never more than the cap inside the window


def test_cap_zero_disables_the_budget():
    r = _FakeRedis()
    assert _run(pb.reserve(r, 0)) == 0.0 and r.z == {}
