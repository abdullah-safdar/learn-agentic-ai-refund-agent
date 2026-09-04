"""A simple fixed-window rate limiter, shared by the chat endpoint (per
client) and the Agent Loop's outbound tool calls (per tool name).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional


class InMemoryRateLimiter:
    """In-process only -- correct for a single backend instance (this
    project's deployment target), but not shared across instances. A
    multi-instance deployment would need a shared store (e.g. Redis)
    instead; swapping that in doesn't change callers, since they only see
    `.allow(key)`.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        hits = self._hits[key]
        cutoff = current - self._window_seconds
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(current)
        return True


# Sized for a single client's chat submission rate.
CHAT_RATE_LIMIT = int(os.environ.get("CHAT_RATE_LIMIT_PER_WINDOW", "10"))
CHAT_RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("CHAT_RATE_LIMIT_WINDOW_SECONDS", "60"))
chat_rate_limiter = InMemoryRateLimiter(CHAT_RATE_LIMIT, CHAT_RATE_LIMIT_WINDOW_SECONDS)

# A separate instance/config from the chat limiter above: the tool-call keys
# ("order_lookup_tool"/"stripe_refund_tool") are global -- shared across
# every customer's request -- so reusing the chat limiter's config would cap
# the entire system's order lookups/Stripe calls to a single client's
# budget.
TOOL_CALL_RATE_LIMIT = int(os.environ.get("TOOL_CALL_RATE_LIMIT_PER_WINDOW", "60"))
TOOL_CALL_RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("TOOL_CALL_RATE_LIMIT_WINDOW_SECONDS", "60"))
tool_call_rate_limiter = InMemoryRateLimiter(TOOL_CALL_RATE_LIMIT, TOOL_CALL_RATE_LIMIT_WINDOW_SECONDS)
