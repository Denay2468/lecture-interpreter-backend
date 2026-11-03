"""A small in-memory sliding-window rate limiter for the sign-in endpoints.

In memory means it resets on restart and isn't shared between server processes. That is fine for one server,
and it should move to Redis (or the reverse proxy) before running several.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_events: int, window_s: float, clock=time.monotonic) -> None:
        self._max = max_events
        self._window = window_s
        self._clock = clock
        self._events: dict = defaultdict(deque)

    def allow(self, key) -> bool:
        """Records an attempt and returns whether it is within the limit."""
        now = self._clock()
        q = self._events[key]
        while q and now - q[0] > self._window:
            q.popleft()
        if len(q) >= self._max:
            return False
        q.append(now)
        if len(self._events) > 20_000:  # keep memory bounded
            for k in [k for k, v in self._events.items() if not v or now - v[-1] > self._window]:
                del self._events[k]
        return True

    def clear(self, key) -> None:
        self._events.pop(key, None)
