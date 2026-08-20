"""Tests for the per-session tail cache.

A fake fetcher models the real API: ascending events, 1000/page, has_more,
after_id (forward) and before_id (backward, inclusive of the boundary event).
"""

from cc_tap.tailcache import TailCache


class FakeApi:
    """In-memory event store paginated like /v1/sessions/{id}/events."""

    def __init__(self, n: int):
        # events[i] uuid = "e{i}", oldest first
        self.events = [{"uuid": f"e{i}", "type": "assistant" if i % 2 else "user", "i": i} for i in range(n)]
        self.calls = 0

    def _index_of(self, uid):
        return next((i for i, e in enumerate(self.events) if e["uuid"] == uid), None)

    def fetch(self, _sid, *, limit=1000, after_id=None, before_id=None):
        self.calls += 1
        evs = self.events
        if before_id is not None:
            j = self._index_of(before_id)
            if j is None:
                raise ValueError("bad before_id")
            lo = max(0, j - limit + 1)
            page = evs[lo : j + 1]  # inclusive of before_id
            has_more = lo > 0
        else:
            start = 0 if after_id is None else self._index_of(after_id) + 1
            page = evs[start : start + limit]
            has_more = start + limit < len(evs)
        return {
            "data": page,
            "has_more": has_more,
            "first_id": page[0]["uuid"] if page else None,
            "last_id": page[-1]["uuid"] if page else None,
        }


def _sync_spawn(cache: TailCache, api: FakeApi):
    """Run the 'background' warm inline so tests are deterministic."""

    def spawn(sid, start_after):
        try:
            newest = cache.walk_to_end(api.fetch, sid, start_after)
            cache.complete_background_warm(sid, newest)
        except Exception:
            cache.abort_background_warm(sid)

    return spawn


class TestSmallSessionInline:
    def test_small_session_served_in_one_shot(self):
        api = FakeApi(50)
        cache = TailCache()
        out = cache.recent_events("s", 5, fetch_page=api.fetch, spawn_background=_sync_spawn(cache, api))
        assert out is not None
        assert [e["uuid"] for e in out] == ["e45", "e46", "e47", "e48", "e49"]
        assert cache.is_indexed("s")

    def test_medium_multipage_finishes_inline(self):
        api = FakeApi(2500)  # 3 pages, well within the time budget
        cache = TailCache()
        out = cache.recent_events("s", 3, fetch_page=api.fetch, spawn_background=_sync_spawn(cache, api))
        assert [e["uuid"] for e in out] == ["e2497", "e2498", "e2499"]

    def test_returns_newest_not_an_older_slice(self):
        api = FakeApi(3000)
        cache = TailCache()
        out = cache.recent_events("s", 1, fetch_page=api.fetch, spawn_background=_sync_spawn(cache, api))
        assert out[-1]["uuid"] == "e2999"  # the genuine newest


class TestLargeSessionDefersThenServes:
    def _tiny_budget_cache(self):
        # clock jumps past the budget after the first page, forcing a defer.
        ticks = iter([0.0] + [100.0] * 50)
        return TailCache(inline_budget_s=1.0, clock=lambda: next(ticks))

    def test_first_read_defers_then_retry_serves_the_tail(self):
        api = FakeApi(12000)
        cache = self._tiny_budget_cache()
        spawn = _sync_spawn(cache, api)  # runs the walk synchronously

        first = cache.recent_events("s", 3, fetch_page=api.fetch, spawn_background=spawn)
        assert first is None  # deferred -> caller shows "indexing, retry"
        assert cache.is_indexed("s")  # sync spawn completed the warm

        second = cache.recent_events("s", 3, fetch_page=api.fetch, spawn_background=spawn)
        assert [e["uuid"] for e in second] == ["e11997", "e11998", "e11999"]

    def test_background_continues_from_where_inline_stopped(self):
        api = FakeApi(12000)
        cache = self._tiny_budget_cache()
        seen_start = {}

        def spawn(sid, start_after):
            seen_start["start_after"] = start_after
            cache.complete_background_warm(sid, cache.walk_to_end(api.fetch, sid, start_after))

        cache.recent_events("s", 3, fetch_page=api.fetch, spawn_background=spawn)
        # inline walked exactly one page (1000 events) before the budget hit
        assert seen_start["start_after"] == "e999"


class TestConcurrencyClaim:
    def test_second_reader_while_warming_gets_none_without_walking(self):
        api = FakeApi(12000)
        ticks = iter([0.0] + [100.0] * 50)
        cache = TailCache(inline_budget_s=1.0, clock=lambda: next(ticks))

        spawned = []

        def spawn(sid, start_after):
            spawned.append(sid)  # do NOT complete — simulate a warm in flight

        assert cache.recent_events("s", 3, fetch_page=api.fetch, spawn_background=spawn) is None
        assert cache.is_warming("s")
        calls_after_first = api.calls

        # second reader must not start its own walk
        assert cache.recent_events("s", 3, fetch_page=api.fetch, spawn_background=spawn) is None
        assert api.calls == calls_after_first
        assert len(spawned) == 1


class TestTypeFilteredTail:
    def test_reads_enough_of_a_filtered_type_paging_backward(self):
        api = FakeApi(5000)
        cache = TailCache()
        spawn = _sync_spawn(cache, api)
        # warm first
        cache.recent_events("s", 1, fetch_page=api.fetch, spawn_background=spawn)
        api.calls = 0
        # want 10 'user' events (the even indices); backward paging must collect them
        out = cache.recent_events("s", 10, fetch_page=api.fetch, keep=lambda e: e["type"] == "user",
                                  spawn_background=spawn)
        assert len(out) == 10
        assert all(e["type"] == "user" for e in out)
        assert out[-1]["uuid"] == "e4998"  # newest user event

    def test_backward_paging_deduplicates_boundary_event(self):
        api = FakeApi(600)  # forces multiple backward pages at TAIL_PAGE_SIZE=200
        cache = TailCache()
        spawn = _sync_spawn(cache, api)
        cache.recent_events("s", 1, fetch_page=api.fetch, spawn_background=spawn)
        out = cache.recent_events("s", 500, fetch_page=api.fetch, spawn_background=spawn)
        uids = [e["uuid"] for e in out]
        assert len(uids) == len(set(uids))  # no duplicates across page boundaries


class TestIncrementalRefresh:
    def test_fast_path_only_fetches_new_events(self):
        api = FakeApi(1500)
        cache = TailCache()
        spawn = _sync_spawn(cache, api)
        cache.recent_events("s", 2, fetch_page=api.fetch, spawn_background=spawn)

        # session grows by 3 events
        api.events.extend({"uuid": f"n{i}", "type": "assistant", "i": 1500 + i} for i in range(3))
        api.calls = 0
        out = cache.recent_events("s", 2, fetch_page=api.fetch, spawn_background=spawn)
        assert out[-1]["uuid"] == "n2"  # newest is picked up
        # refresh(after_id) + a bounded backward read — a handful of calls, not a full walk
        assert api.calls <= 4
