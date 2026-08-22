"""A minimal in-memory rate limiter, used by web.py to cap how often any one
visitor can send a chat message — every message triggers a metered
Anthropic API call and, potentially, a leave_contact_request write, so this
is the main defense against a scripted flood running up the API bill or
spamming fake contact requests.

Caveat: this is per-process, in-memory state. It resets on restart and does
NOT coordinate across multiple replicas/workers — fine for a single-instance
deployment (which is what this app is), not a substitute for a shared store
(e.g. Redis) if you ever scale this out horizontally.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    """Thread-safe sliding-window rate limiter: at most `max_calls` calls per
    `period_seconds`, tracked separately per identifier (e.g. a logged-in
    username or an IP address).
    """

    def __init__(self, max_calls: int, period_seconds: float) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be at least 1")
        if period_seconds <= 0:
            raise ValueError("period_seconds must be positive")
        self._max_calls = max_calls
        self._period = period_seconds
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, identifier: str) -> bool:
        """Record and allow a call for `identifier` if under the limit;
        otherwise reject it (and don't count the rejected attempt).
        """
        now = time.monotonic()
        with self._lock:
            calls = self._calls[identifier]
            while calls and now - calls[0] > self._period:
                calls.popleft()
            if len(calls) >= self._max_calls:
                return False
            calls.append(now)
            return True

    def retry_after_seconds(self, identifier: str) -> int:
        """Roughly how long until `identifier` can make another call —
        for a friendly "try again in N seconds" message, not for precise
        scheduling.
        """
        with self._lock:
            calls = self._calls[identifier]
            if not calls:
                return 0
            remaining = self._period - (time.monotonic() - calls[0])
            return max(0, round(remaining))
