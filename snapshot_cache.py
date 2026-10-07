"""The shared data files as last read from GitHub, kept fresh without making
anyone wait.

Measured on 2026-10-07: reading companies.json, settings.json and
seen_jobs.json through the GitHub API takes about 2.5 s (four sequential
requests). As a 5-minute st.cache_data that cost landed on whichever click
came after the cache expired — the app froze with no feedback.

Here a click always gets the last good copy at once. Once that copy is
older than ``ttl_s`` a single background refresh is started; the next page
load sees the new data. Only the very first read of a server process waits
(there is nothing to show yet), and an explicit Refresh still waits, because
the person asked for the latest data.

No Streamlit import here, so this is unit-testable on its own.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("snapshot_cache")


class SnapshotCache:
    def __init__(self, fetch, ttl_s: float = 300, clock=time.time, spawn=None):
        """``fetch()`` returns {file name: parsed content}; a file it couldn't
        read is simply missing (the last good copy of it is kept).
        ``spawn(fn)`` runs fn in the background (a daemon thread by default)."""
        self._fetch, self.ttl_s, self._clock = fetch, ttl_s, clock
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._data: dict | None = None
        self._at = 0.0
        self._refreshing = False
        self._put_at: dict[str, float] = {}       # when the app last saved each file
        self._lock = threading.Lock()

    def _load(self) -> None:
        started = self._clock()
        try:
            fresh = self._fetch() or {}
        except Exception as e:                     # never let a refresh break a page
            log.warning("refreshing shared data failed: %s", type(e).__name__)
            fresh = {}
        with self._lock:
            # a file the app saved after this read began is newer than what it read
            fresh = {k: v for k, v in fresh.items() if self._put_at.get(k, float("-inf")) < started}
            self._data = {**(self._data or {}), **fresh}
            self._at = self._clock()

    def _background(self) -> None:
        try:
            self._load()
        finally:
            with self._lock:
                self._refreshing = False

    def get(self) -> dict:
        """The current copy — immediately, except for the first read."""
        with self._lock:
            have = self._data is not None
        if not have:
            self._load()
        with self._lock:
            start = not self._refreshing and self._clock() - self._at > self.ttl_s
            if start:
                self._refreshing = True
        if start:
            self._spawn(self._background)
        with self._lock:
            return dict(self._data or {})

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
