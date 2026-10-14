"""The shared data files as last read from GitHub, kept fresh without making
anyone wait.

Measured on 2026-10-07: reading companies.json, settings.json and
seen_jobs.json through the GitHub API takes about 2.5 s (four sequential
requests). As a 5-minute st.cache_data that cost landed on whichever click
came after the cache expired — the app froze with no feedback.

Here a click gets the last good copy at once while it is younger than
``max_stale_s``. Once it is older than ``ttl_s`` a single background refresh
is started; the next page load sees the new data. An explicit Refresh waits,
because the person asked for the latest data.

The copy is never served past ``max_stale_s`` (P0, 2026-10-12): the server
process outlives idle hours, and the first page after them showed a day-old
"Last scan" and "Scans delayed" until Refresh. A copy that old is read again
before the page is drawn — that one page waits, about as long as a Refresh.
If GitHub can't be read, the old copy is still shown (and ``stale()`` says
so), retried at most every ``retry_s`` so a GitHub outage can't make every
click wait.

No Streamlit import here, so this is unit-testable on its own.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("snapshot_cache")


class SnapshotCache:
    def __init__(self, fetch, ttl_s: float = 300, clock=time.time, spawn=None,
                 max_stale_s: float = 900, retry_s: float = 30):
        """``fetch()`` returns {file name: parsed content}; a file it couldn't
        read is simply missing (the last good copy of it is kept).
        ``spawn(fn)`` runs fn in the background (a daemon thread by default)."""
        self._fetch, self.ttl_s, self._clock = fetch, ttl_s, clock
        self.max_stale_s, self.retry_s = max_stale_s, retry_s
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._data: dict | None = None
        self._at = 0.0                             # last read attempt (drives the background refresh)
        self._ok_at: float | None = None           # last read that returned data
        self._refreshing = False
        self._put_at: dict[str, float] = {}       # when the app last saved each file
        self._lock = threading.Lock()
        self._sync = threading.Lock()              # one blocking re-read at a time

    def _load(self) -> None:
        started = self._clock()
        try:
            fresh = self._fetch() or {}
        except Exception as e:                     # never let a refresh break a page
            log.warning("refreshing shared data failed: %s", type(e).__name__)
            fresh = {}
        with self._lock:
            got = bool(fresh)
            # a file the app saved after this read began is newer than what it read
            fresh = {k: v for k, v in fresh.items() if self._put_at.get(k, float("-inf")) < started}
            self._data = {**(self._data or {}), **fresh}
            self._at = self._clock()
            if got:
                self._ok_at = self._at

    def _background(self) -> None:
        try:
            self._load()
        finally:
            with self._lock:
                self._refreshing = False

    def _too_old(self) -> bool:
        """The copy is past max_stale_s and no read was tried in the last
        retry_s (caller holds the lock)."""
        now = self._clock()
        good = self._ok_at if self._ok_at is not None else float("-inf")
        return now - good > self.max_stale_s and now - self._at > self.retry_s

    def get(self) -> dict:
        """The current copy — immediately, unless it is the first read or the
        copy is too old to show (then this one call waits for GitHub)."""
        with self._lock:
            have = self._data is not None
        if not have:
            self._load()
        else:
            with self._lock:
                too_old = self._too_old()
            if too_old:
                with self._sync:
                    with self._lock:
                        too_old = self._too_old()      # another session may have just read it
                    if too_old:
                        self._load()
        with self._lock:
            start = not self._refreshing and self._clock() - self._at > self.ttl_s
            if start:
                self._refreshing = True
        if start:
            self._spawn(self._background)
        with self._lock:
            return dict(self._data or {})

    def has_data(self) -> bool:
        """True once a first read finished (a get() then returns at once)."""
        with self._lock:
            return self._data is not None

    def stale(self) -> bool:
        """True when what get() returns is older than max_stale_s because
        GitHub couldn't be read (shown as such, never as fresh)."""
        with self._lock:
            return self._ok_at is not None and self._clock() - self._ok_at > self.max_stale_s

    def refresh_now(self) -> dict:
        """Read GitHub again and wait for it (an explicit Refresh)."""
        self._load()
        with self._lock:
            return dict(self._data or {})

    def put(self, name: str, content) -> None:
        """The app just saved ``name``: show the saved version right away
        (a refresh in flight can't bring back the older copy)."""
        with self._lock:
            self._data = {**(self._data or {}), name: content}
            self._put_at[name] = self._clock()
