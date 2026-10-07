"""The shared-data cache: a click never waits for GitHub once data is there."""
from snapshot_cache import SnapshotCache


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _cache(results, clock, spawned):
    calls = []

    def fetch():
        calls.append(clock.t)
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return SnapshotCache(fetch, ttl_s=300, clock=clock, spawn=spawned.append), calls


def test_first_read_waits_then_reads_come_from_the_copy():
    clock, spawned = Clock(), []
    c, calls = _cache([{"a.json": 1}], clock, spawned)
    assert c.get() == {"a.json": 1} and len(calls) == 1
    clock.t += 299
    assert c.get() == {"a.json": 1} and len(calls) == 1 and spawned == []


def test_a_stale_copy_is_served_at_once_and_refreshed_in_the_background():
    clock, spawned = Clock(), []
    c, calls = _cache([{"a.json": 1}, {"a.json": 2}], clock, spawned)
    c.get()
    clock.t += 301
    assert c.get() == {"a.json": 1}                  # no waiting for GitHub
    assert len(spawned) == 1 and len(calls) == 1
    assert c.get() == {"a.json": 1} and len(spawned) == 1    # one refresh at a time
    spawned[0]()                                     # the background refresh runs
    assert c.get() == {"a.json": 2} and len(calls) == 2


def test_a_failed_refresh_keeps_the_last_good_copy():
    clock, spawned = Clock(), []
    c, _ = _cache([{"a.json": 1, "b.json": 1}, RuntimeError("github down"), {"b.json": 3}], clock, spawned)
    c.get()
    clock.t += 301
    c.get()
    spawned.pop()()
    assert c.get() == {"a.json": 1, "b.json": 1}
    assert c.refresh_now() == {"a.json": 1, "b.json": 3}     # a file missing from a read is kept


def test_a_save_wins_over_a_refresh_that_started_before_it():
    clock, spawned = Clock(), []
    holder = {}

    def fetch():
        if "c" in holder:                            # the save lands while GitHub is being read
            clock.t += 1
            holder["c"].put("a.json", "saved")
        return {"a.json": "old", "b.json": "new"}
    c = SnapshotCache(fetch, ttl_s=300, clock=clock, spawn=spawned.append)
    c.get()
    holder["c"] = c
    assert c.refresh_now() == {"a.json": "saved", "b.json": "new"}


def test_put_shows_the_saved_version_immediately():
    clock, spawned = Clock(), []
    c, calls = _cache([{"a.json": 1}], clock, spawned)
    c.get()
    c.put("a.json", 5)
    assert c.get() == {"a.json": 5} and len(calls) == 1


def test_callers_get_a_copy_not_the_cache_itself():
    clock, spawned = Clock(), []
    c, _ = _cache([{"a.json": 1}], clock, spawned)
    c.get()["a.json"] = "mutated"
    assert c.get() == {"a.json": 1}
