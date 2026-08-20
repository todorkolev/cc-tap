"""Per-session tail cache for reading the newest events of huge sessions.

Why this exists
---------------
The events API is ascending-only, capped at 1000 per page, with no total, no
descending order, and no cheap "newest" pointer (offset is ignored; a made-up
cursor 400s; ``get_session`` carries no event id). So the *only* way to locate
the tail of a session is to walk forward from the start — O(number of events).

``read_session``/``get_session_events`` only want the last handful of events,
but that forward walk made them O(total). A 12k-event session took ~30s, which
blows past the relay's per-call timeout: the caller sees a bare
"MCP tool call failed" after a long hang, while smaller sessions read fine.

The fix, in three parts:

1. **Warm once.** The first read of a session walks forward to discover the
   newest event id. Small/medium sessions finish inside a time budget and are
   served inline — no behaviour change. A session too large to finish in budget
   hands the rest of the walk to a background thread and the caller gets an
   honest "indexing, retry shortly" instead of a 30s hang.
2. **Maintain cheaply.** Once the newest id is known it is kept current by
   fetching only events *after* it (usually nothing), which is O(new events).
3. **Read the tail cheaply.** With a known newest id, the last N events come
   from paging *backward* with ``before_id`` — a couple of fast requests,
   independent of session size.

State is a module-level dict in the single uvicorn worker. It is a cache: lost
on restart, rebuilt on next read. A lock guards the bookkeeping so concurrent
reads of the same session trigger at most one warm walk.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: How long the first (inline) read may spend walking before it gives up and
#: hands the rest to the background. Kept well under the relay timeout.
INLINE_WARM_BUDGET_S = 8.0

#: Events kept from the inline walk so a session that finishes in budget can be
#: served without a second round-trip. One page is ample for any last_n.
INLINE_TAIL_WINDOW = 1000

#: Page size for the backward tail read. Small: we usually want ~20-50 events.
TAIL_PAGE_SIZE = 200

#: How many backward pages to walk before giving up on collecting `want` of a
#: sparse event type — bounds the cost of a filtered tail read.
TAIL_MAX_PAGES = 8

#: A warmed cursor older than this is re-validated by walking forward from it;
#: that walk is cheap (usually one empty page) and keeps a long-lived process
#: from trusting a very stale newest id. Purely a belt-and-braces bound.
_STALE_S = 3600

#: Callable that fetches one raw event page. Signature mirrors
#: CCRClient.get_events_page: (sid, *, limit, after_id=None, before_id=None) -> body.
PageFetcher = Callable[..., dict]


class _Entry:
    __slots__ = ("newest_id", "updated_at")

    def __init__(self, newest_id: str, updated_at: float):
        self.newest_id = newest_id
        self.updated_at = updated_at


class TailCache:
    """Indexes sessions by their newest event id and serves recent events."""

    def __init__(
        self,
        *,
        inline_budget_s: float = INLINE_WARM_BUDGET_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._index: dict[str, _Entry] = {}
        self._warming: set[str] = set()
        self._lock = threading.Lock()
        self._inline_budget_s = inline_budget_s
        self._clock = clock

    # --- public API ------------------------------------------------------

    def recent_events(
        self,
        sid: str,
        want: int,
        *,
        fetch_page: PageFetcher,
        keep: Callable[[dict], bool] | None = None,
        spawn_background: Callable[[str, str | None], None],
    ) -> list[dict] | None:
        """Return recent events for ``sid``, newest last, or None if warming.

        ``want`` is how many events matching ``keep`` the caller needs (the tail
        read stops early once it has them). None means "not indexed yet, a warm
        is in flight — retry shortly"; the caller renders the honest message.

        ``fetch_page`` performs one page fetch (already wrapped with auth retry).
        ``spawn_background(sid, start_after)`` launches the background warm; it is
        injected so this module stays free of threading/client wiring and stays
        unit-testable.
        """
        keep = keep or (lambda _e: True)

        with self._lock:
            entry = self._index.get(sid)
            if entry is None:
                if sid in self._warming:
                    return None  # someone is already warming it
                self._warming.add(sid)  # claim the warm

        if entry is not None:
            # Fast path: refresh the cursor to the true end, then read backward.
            newest = self._refresh(sid, entry, fetch_page)
            return self._read_backward(sid, newest, want, keep, fetch_page)

        # We hold the warm claim. Walk inline within budget.
        try:
            result = self._walk(
                fetch_page,
                sid,
                start_after=None,
                deadline=self._clock() + self._inline_budget_s,
                keep_tail=INLINE_TAIL_WINDOW,
            )
        except Exception:
            with self._lock:
                self._warming.discard(sid)
            raise

        if result["done"]:
            newest = result["newest_id"]
            with self._lock:
                if newest:
                    self._index[sid] = _Entry(newest, time.time())
                self._warming.discard(sid)
            tail = result["tail"] or []
            return _take_last(tail, want, keep)

        # Too large to finish inline. Hand the rest to the background (keeping the
        # warm claim, which the background clears when done) and ask for a retry.
        logger.info("Session %s too large to index inline (%d pages so far); warming in background",
                    sid, result["pages"])
        spawn_background(sid, result["cursor"])
        return None

    def complete_background_warm(self, sid: str, newest_id: str | None) -> None:
        """Record the result of a background warm and release the claim."""
        with self._lock:
            if newest_id:
                self._index[sid] = _Entry(newest_id, time.time())
            self._warming.discard(sid)

    def abort_background_warm(self, sid: str) -> None:
        """Release the warm claim after a failed background walk, so it retries."""
        with self._lock:
            self._warming.discard(sid)

    def walk_to_end(self, fetch_page: PageFetcher, sid: str, start_after: str | None) -> str | None:
        """Walk forward to the end (no budget); return the newest event id.

        Used by the background warmer, continuing from where the inline walk
        stopped so no work is repeated.
        """
        return self._walk(fetch_page, sid, start_after=start_after, deadline=None, keep_tail=0)["newest_id"]

    # --- internals -------------------------------------------------------

    def _refresh(self, sid: str, entry: _Entry, fetch_page: PageFetcher) -> str:
        """Extend a known cursor to the true end by fetching only new events."""
        walked = self._walk(fetch_page, sid, start_after=entry.newest_id, deadline=None, keep_tail=0)
        newest = walked["newest_id"] or entry.newest_id
        with self._lock:
            # Only advance; never move the cursor backward on a racing update.
            cur = self._index.get(sid)
            if cur is None or newest != cur.newest_id:
                self._index[sid] = _Entry(newest, time.time())
        return newest

    def _read_backward(
        self,
        sid: str,
        newest_id: str,
        want: int,
        keep: Callable[[dict], bool],
        fetch_page: PageFetcher,
    ) -> list[dict]:
        """Collect the tail by paging backward from ``newest_id`` with before_id."""
        collected: deque[dict] = deque()
        seen: set[str] = set()
        cursor = newest_id
        for _ in range(TAIL_MAX_PAGES):
            body = fetch_page(sid, limit=TAIL_PAGE_SIZE, before_id=cursor)
            batch = body.get("data", [])
            if not batch:
                break
            # Prepend older events, de-duplicating (before_id includes its own
            # event, which reappears as the last item of the next page back).
            for ev in reversed(batch):
                uid = ev.get("uuid")
                if uid in seen:
                    continue
                seen.add(uid)
                collected.appendleft(ev)
            if sum(1 for e in collected if keep(e)) >= want:
                break
            first_id = batch[0].get("uuid")
            if not first_id or first_id == cursor:
                break  # not advancing
            cursor = first_id
        return _take_last(list(collected), want, keep)

    def _walk(
        self,
        fetch_page: PageFetcher,
        sid: str,
        *,
        start_after: str | None,
        deadline: float | None,
        keep_tail: int,
    ) -> dict:
        """Forward-walk from ``start_after``.

        Returns ``{done, newest_id, cursor, tail, pages}``. ``done`` is False
        only when a deadline was set and hit before the end; ``cursor`` is then
        where to resume. ``tail`` holds the last ``keep_tail`` events seen (only
        meaningful when ``done`` — otherwise it is the middle of the history).
        """
        cursor = start_after
        newest = start_after
        tail: deque[dict] | None = deque(maxlen=keep_tail) if keep_tail else None
        pages = 0
        while True:
            body = fetch_page(sid, limit=1000, after_id=cursor)
            batch = body.get("data", [])
            pages += 1
            if batch:
                newest = body.get("last_id") or batch[-1].get("uuid")
                if tail is not None:
                    tail.extend(batch)
                cursor = newest
            if not body.get("has_more") or not batch:
                return {"done": True, "newest_id": newest, "cursor": cursor,
                        "tail": list(tail) if tail is not None else None, "pages": pages}
            if deadline is not None and self._clock() >= deadline:
                return {"done": False, "newest_id": newest, "cursor": cursor,
                        "tail": list(tail) if tail is not None else None, "pages": pages}

    # --- introspection (for tests / diagnostics) -------------------------

    def is_indexed(self, sid: str) -> bool:
        with self._lock:
            return sid in self._index

    def is_warming(self, sid: str) -> bool:
        with self._lock:
            return sid in self._warming


def _take_last(events: list[dict], want: int, keep: Callable[[dict], bool]) -> list[dict]:
    """Last ``want`` events matching ``keep``, in chronological order."""
    matching = [e for e in events if keep(e)]
    return matching[-want:] if want else matching
