"""The Agent Loop: resolves a RefundRequest end-to-end -- Order Lookup ->
Policy check -> Stripe Refund. `run()` never persists anything itself; the
caller (chat_api.py) writes the resolved outcome back onto the RefundRequest
row via db.update_refund_status() after run() returns.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional, Union

import db
from models import (
    AgentResult,
    Completed,
    Escalated,
    Failed,
    NewRequestInput,
    Order,
    RefundRequest,
    ResumeInput,
    RunInput,
)
from services import policy, stripe_refund

# Ask First: the Order Lookup Tool's retry/step cap has no confirmed value
# anywhere else, so this picks a sensible default. The Stripe Refund Tool
# is deliberately NOT covered by this cap -- see `_call_stripe` below.
ORDER_LOOKUP_RETRY_CAP = 3

# A later stage owns real Confidence Score computation and the full
# escalation UX -- until then, every Escalated outcome carries the same
# generic placeholder recommendation.
_GENERIC_TENTATIVE_RECOMMENDATION = (
    "Needs manual review -- this request did not pass the automatic "
    "policy checks."
)


class AgentLoopFailedError(Exception):
    """Raised by the caller (chat_api.py) when run() resolves to Failed --
    translated to a 502 `resolution_failed` envelope by errors.py. run()
    itself never raises this; it only ever returns a Failed value."""


# --------------------------------------------------------------------------
# Injection-pattern pre-check (OWASP Top 10 for LLM Applications). Checked
# against whichever field actually feeds into the tool call about to be
# built -- order_reference for the Order Lookup Tool's query, reason for
# the Stripe Refund Tool's metadata.
# --------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"ignore\s+(all|any|the)?\s*(previous|prior|above)\s+instructions",
        r"disregard\s+(all|any|the)?\s*(previous|prior|above)",
        r"system\s*prompt",
        r"you\s+are\s+now\b",
        r"new\s+instructions\s*:",
        r"reveal\s+(your|the)\s+(instructions|prompt|system)",
        r"act\s+as\s+(if|a)\b",
        r";\s*drop\s+table\b",
        r";\s*delete\s+from\b",
        r"\bunion\s+select\b",
        r"--\s*$",
    ]
]


def _matches_injection_pattern(value: str) -> bool:
    """True if `value` matches a known injection pattern -- checked before
    a tool call is built, never after."""
    return any(pattern.search(value) for pattern in _INJECTION_PATTERNS)


def _escalated_for(refund_request: RefundRequest) -> Escalated:
    """Every Escalated outcome here uses the RefundRequest's own id as
    pending_review_id -- there's no separate approval-queue table yet, so
    the RefundRequest row itself is the thing a future reviewer acts on."""
    return Escalated(
        pending_review_id=refund_request.id,
        tentative_recommendation=_GENERIC_TENTATIVE_RECOMMENDATION,
    )


def _resolve_order(refund_request: RefundRequest, rate_limiter) -> tuple[bool, Optional[Order]]:
    """Order Lookup Tool: retried up to ORDER_LOOKUP_RETRY_CAP on failure.
    Returns (succeeded, order) -- succeeded=False means every attempt was
    rejected/failed and the retry cap was exhausted; the caller must
    escalate, never treat this as Failed.

    `rate_limiter` just needs an `.allow(key) -> bool` method -- any object
    with that shape works, no formal interface required (like passing any
    object satisfying a TypeScript structural type, minus the compiler
    check).
    """
    for _ in range(ORDER_LOOKUP_RETRY_CAP):
        if _matches_injection_pattern(refund_request.order_reference):
            # Tool call is never built; this attempt still counts toward
            # the retry cap.
            continue
        if not rate_limiter.allow("order_lookup_tool"):
            continue
        try:
            order = db.find_order_by_reference(refund_request.order_reference)
        except Exception:  # noqa: BLE001 -- any I/O failure is retryable here
            continue
        return True, order

    return False, None


def _call_stripe(
    refund_request: RefundRequest,
    order: Order,
    amount_cents: int,
    rate_limiter,
) -> Union[Completed, Escalated]:
    """Stripe Refund Tool: exactly one attempt, never retried -- a retry
    after an ambiguous-outcome failure (e.g. a timeout on an
    already-successful charge) could issue a second real refund with no
    idempotency key yet to catch it."""
    if _matches_injection_pattern(refund_request.reason):
        return _escalated_for(refund_request)
    if not rate_limiter.allow("stripe_refund_tool"):
        return _escalated_for(refund_request)

    try:
        refund_id = stripe_refund.issue_refund(order=order, amount_cents=amount_cents, reason=refund_request.reason)
    except Exception:  # noqa: BLE001 -- any failure escalates immediately, never retried
        return _escalated_for(refund_request)

    return Completed(refund_id=refund_id, amount_cents=amount_cents)


def _resolve_requested_amount_cents(refund_request: RefundRequest, order: Optional[Order]) -> Optional[int]:
    """amount_cents may be null on the RefundRequest (unstated at intake) --
    treated as "full refund of the order's amount" when an order was
    actually found."""
    if refund_request.amount_cents is not None:
        return refund_request.amount_cents
    if order is not None:
        return order.amount_cents
    return None


def _run_new_request(refund_request: RefundRequest, rate_limiter, now: datetime) -> AgentResult:
    lookup_succeeded, order = _resolve_order(refund_request, rate_limiter)
    if not lookup_succeeded:
        return _escalated_for(refund_request)

    requested_amount_cents = _resolve_requested_amount_cents(refund_request, order)

    decision = policy.evaluate_policy(order, requested_amount_cents, now)
    if not decision.compliant:
        return _escalated_for(refund_request)

    if order is None or requested_amount_cents is None:
        # A compliant decision without a resolvable order/amount would mean
        # evaluate_policy() violated its own contract -- not a normal
        # business outcome, so this surfaces as Failed rather than a
        # silently-wrong Completed/Escalated.
        raise AssertionError("evaluate_policy() returned compliant=True without a resolvable order/amount")

    return _call_stripe(refund_request, order, requested_amount_cents, rate_limiter)


def run(input: RunInput, rate_limiter, now: Optional[datetime] = None) -> AgentResult:
    """The Agent Loop's single entry point. `RunInput` covers both forward
    execution (NewRequestInput, today) and resuming after Escalation
    (ResumeInput, not handled yet) through the same entry point -- never a
    second method.

    Any unexpected failure (a policy bug, a defensive assertion, ...) is
    caught here and surfaces as Failed rather than propagating -- run()
    never raises for a resolvable RunInput. Order Lookup and Stripe
    failures are handled by their own tool-specific try/except blocks
    inside the resolution path above and never reach here as exceptions;
    only a genuine bug does.
    """
    if isinstance(input, ResumeInput):
        raise NotImplementedError("ResumeInput handling isn't wired up yet")
    if not isinstance(input, NewRequestInput):
        raise TypeError(f"Unsupported RunInput variant: {type(input)!r}")

    current_time = now or datetime.now(timezone.utc)

    try:
        return _run_new_request(input.refund_request, rate_limiter, current_time)
    except Exception as exc:  # noqa: BLE001 -- translate any unexpected failure to Failed
        return Failed(reason=str(exc))
