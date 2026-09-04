"""Policy/compliance check: does this refund request pass the store's
rules? No I/O -- pure function.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from models import ORDER_STATUS_COMPLETED, Order, PolicyDecision

# Ask First: no confirmed return-window duration exists anywhere else;
# defaulting to 30 days from the order date.
RETURN_WINDOW_DAYS = 30

# The hardcoded rules below are deterministic yes/no checks, not a
# probabilistic judgment -- there's no partial-confidence case to express,
# so this always reports full confidence in whichever way it decided.
_FULL_CONFIDENCE = 1.0


def _parse_iso8601_utc(value: str) -> datetime:
    # order_date always carries a trailing "Z" (db.py) -- normalize to the
    # "+00:00" form datetime.fromisoformat understands.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _non_compliant() -> PolicyDecision:
    return PolicyDecision(compliant=False, confidence=_FULL_CONFIDENCE, citation_ids=[])


def evaluate_policy(order: Optional[Order], requested_amount_cents: Optional[int], now: datetime) -> PolicyDecision:
    """The four hardcoded compliance rules: order exists and is completed,
    request is within the return window, requested amount is a positive
    integer, and does not exceed the order's amount.

    This is a placeholder for a later stage's real AI-driven check: an LLM
    reasoning over the store's actual policy documents (with citations)
    instead of these four `if` statements, behind this exact same function
    signature -- swapping the body is all that will change.
    """
    if order is None or order.status != ORDER_STATUS_COMPLETED:
        return _non_compliant()

    order_date = _parse_iso8601_utc(order.order_date)
    if now > order_date + timedelta(days=RETURN_WINDOW_DAYS):
        return _non_compliant()

    if requested_amount_cents is None or requested_amount_cents <= 0:
        return _non_compliant()

    if requested_amount_cents > order.amount_cents:
        return _non_compliant()

    return PolicyDecision(compliant=True, confidence=_FULL_CONFIDENCE, citation_ids=[])
