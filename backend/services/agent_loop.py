"""The Agent Loop: resolves a RefundRequest end-to-end -- Order Lookup ->
Policy check -> Stripe Refund. `run()` never persists anything itself; the
caller (chat_api.py) writes the resolved outcome back onto the RefundRequest
row via db.update_refund_status() after run() returns.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

import db
from models import (
    STATUS_COMPLETED,
    STATUS_DENIED,
    STATUS_ESCALATED,
    STATUS_FAILED,
    STEP_TYPE_ORDER_LOOKUP,
    STEP_TYPE_OUTCOME,
    STEP_TYPE_POLICY_DECISION,
    STEP_TYPE_REVIEWER_DECISION,
    STEP_TYPE_STRIPE_REFUND,
    AgentResult,
    Completed,
    Denied,
    Escalated,
    Failed,
    NewRequestInput,
    Order,
    RefundRequest,
    ResumeInput,
    RunInput,
)
from services import policy, stripe_refund

logger = logging.getLogger(__name__)

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


def _record_step(refund_request_id: str, step_type: str, step_data: Dict[str, Any], now: datetime) -> None:
    """Writes one TrajectoryEvent row for this Agent Loop step (AD-5). The
    domain (this module) builds the already-redacted, allowlisted
    `step_data` dict -- db.py only persists whatever it's handed, never
    shapes it. Never passed a raw exception, Tool payload, or the
    customer-supplied `reason`.

    Trajectory writes must never affect the resolved AgentResult: a DB
    hiccup or a sequence_no race (the UNIQUE-constraint backstop firing) is
    logged and swallowed here, never propagated -- observability is
    strictly secondary to the real business outcome (an already-Completed
    refund, money already moved via Stripe, must never be turned into a
    Failed response by a logging side-write). This is the only place that
    guard lives; every call site below relies on it.
    """
    try:
        db.record_trajectory_event(refund_request_id, step_type, step_data, now)
    except Exception:  # noqa: BLE001 -- trajectory logging must never affect the AgentResult
        logger.warning(
            "Failed to record TrajectoryEvent for refund_request_id=%r step_type=%r step_data=%r",
            refund_request_id,
            step_type,
            step_data,
            exc_info=True,
        )


def _status_for(result: AgentResult) -> str:
    if isinstance(result, Completed):
        return STATUS_COMPLETED
    if isinstance(result, Escalated):
        return STATUS_ESCALATED
    if isinstance(result, Failed):
        return STATUS_FAILED
    if isinstance(result, Denied):
        return STATUS_DENIED
    raise TypeError(f"Unhandled AgentResult variant: {type(result)!r}")


def _idempotency_key_for(refund_request: RefundRequest) -> str:
    """Deterministic Idempotency Key for the Stripe Refund Tool call,
    derived from `refund_request.id` alone (never `Order` -- per AD-3, a
    key derived from the shared Order would let one request's key block a
    different, legitimate request against the same Order). Stable across
    separate `_call_stripe()` invocations for the same RefundRequest, so a
    crash-then-manual-replay or retried call within Stripe's
    idempotency-key retention window is deduplicated by Stripe itself."""
    return f"refund-request:{refund_request.id}"


def _resolve_order(refund_request: RefundRequest, rate_limiter) -> tuple[bool, Optional[Order], int]:
    """Order Lookup Tool: retried up to ORDER_LOOKUP_RETRY_CAP on failure.
    Returns (succeeded, order, attempts) -- succeeded=False means every
    attempt was rejected/failed and the retry cap was exhausted; the caller
    must escalate, never treat this as Failed. `attempts` is the number of
    attempts actually made (including injection-blocked/rate-limited ones,
    which still count toward the cap) -- the trajectory's `retries_used` is
    `attempts - 1`.

    `rate_limiter` just needs an `.allow(key) -> bool` method -- any object
    with that shape works, no formal interface required (like passing any
    object satisfying a TypeScript structural type, minus the compiler
    check).
    """
    attempts = 0
    for _ in range(ORDER_LOOKUP_RETRY_CAP):
        attempts += 1
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
        return True, order, attempts

    return False, None, attempts


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

    idempotency_key = _idempotency_key_for(refund_request)
    try:
        refund_id = stripe_refund.issue_refund(
            order=order,
            amount_cents=amount_cents,
            reason=refund_request.reason,
            idempotency_key=idempotency_key,
        )
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
    lookup_succeeded, order, attempts = _resolve_order(refund_request, rate_limiter)
    _record_step(
        refund_request.id,
        STEP_TYPE_ORDER_LOOKUP,
        {
            "found": lookup_succeeded and order is not None,
            "retries_used": attempts - 1,
            # Distinguishes "the Order Lookup Tool itself failed/was
            # blocked and the retry cap was exhausted" from "the tool
            # succeeded but genuinely found no matching order" -- both
            # collapse to found=false, but they're different explanations
            # for a debug trajectory to lose.
            "lookup_succeeded": lookup_succeeded,
        },
        now,
    )
    if not lookup_succeeded:
        return _escalated_for(refund_request)

    requested_amount_cents = _resolve_requested_amount_cents(refund_request, order)

    decision = policy.evaluate_policy(order, requested_amount_cents, now)
    _record_step(
        refund_request.id,
        STEP_TYPE_POLICY_DECISION,
        {
            "compliant": decision.compliant,
            "confidence": decision.confidence,
            "citation_ids": decision.citation_ids,
        },
        now,
    )
    if not decision.compliant:
        return _escalated_for(refund_request)

    if order is None or requested_amount_cents is None:
        # A compliant decision without a resolvable order/amount would mean
        # evaluate_policy() violated its own contract -- not a normal
        # business outcome, so this surfaces as Failed rather than a
        # silently-wrong Completed/Escalated.
        raise AssertionError("evaluate_policy() returned compliant=True without a resolvable order/amount")

    try:
        threshold = db.get_current_escalation_threshold(now)
    except Exception:  # noqa: BLE001 -- an unreadable threshold must escalate, never fall through to auto-approval
        logger.warning(
            "Failed to read EscalationThreshold for refund_request_id=%r -- escalating",
            refund_request.id,
            exc_info=True,
        )
        return _escalated_for(refund_request)

    # AD-9: the dollar cutoff is inclusive (`>=`) and always wins regardless
    # of confidence; a low Confidence Score (AD-8, reused from PolicyDecision
    # -- no new LLM-judge call) escalates independently. Either condition on
    # its own is enough. The confidence check is intentionally exclusive at
    # the boundary (`<`, not `<=`) -- confidence exactly equal to the
    # threshold does NOT escalate, matching the epic AC's "falls below"
    # wording, as distinct from the dollar side's "at or above".
    if (
        requested_amount_cents >= threshold.dollar_threshold_cents
        or decision.confidence < threshold.confidence_threshold
    ):
        return _escalated_for(refund_request)

    stripe_result = _call_stripe(refund_request, order, requested_amount_cents, rate_limiter)
    if isinstance(stripe_result, Completed):
        stripe_step_data: Dict[str, Any] = {
            "outcome": "completed",
            "refund_id": stripe_result.refund_id,
            "amount_cents": stripe_result.amount_cents,
        }
    else:
        # Injection-blocked, rate-limited, or a raised Stripe failure --
        # never the raw exception or Stripe payload, only the fact that it
        # escalated.
        stripe_step_data = {"outcome": "escalated"}
    _record_step(refund_request.id, STEP_TYPE_STRIPE_REFUND, stripe_step_data, now)

    return stripe_result


def _resume_request(
    refund_request_id: str,
    reviewer_decision: str,
    reviewer_identifier: str,
    rate_limiter,
    now: datetime,
) -> AgentResult:
    """Resume after a human reviewer's decision on an Escalated request
    (spec-1-6). The reviewer_decision step is recorded first, before either
    branch proceeds, so the trajectory shows who decided what even if
    everything after it fails.

    `reviewer_decision` must be "approve" or "deny" -- already validated by
    the caller (routes/approvals.py) before this is ever called, but
    re-checked here too (raising ValueError for anything else) as
    defense-in-depth at the domain boundary itself: silently treating any
    unrecognized value as "deny" would be a much worse failure mode than an
    explicit error.

    Deny is terminal in one step -- no Stripe call, no order lookup.

    Approve re-attempts through the *same* `_resolve_order`/`_call_stripe`
    path `_run_new_request` uses above -- no new Stripe logic, and
    deliberately no re-run of `policy.evaluate_policy()`/the Escalation
    Threshold check: a human already overrode those automatic checks by
    approving. A resume that fails order lookup, finds no order, can't
    resolve an amount, or fails at Stripe still returns Escalated -- never
    auto-denies, never Failed for a business-as-usual retry-exhaustion.
    """
    if reviewer_decision not in ("approve", "deny"):
        raise ValueError(f"reviewer_decision must be 'approve' or 'deny', got {reviewer_decision!r}.")

    refund_request = db.find_refund_request_by_id(refund_request_id)
    if refund_request is None:
        # Not a normal business outcome -- the caller (routes/approvals.py)
        # already resolved refund_request_id to a real row via
        # db.record_reviewer_decision()'s atomic guard before ever calling
        # run(), so this would mean that row vanished between the decision
        # write and this read. Surfaces as Failed via run()'s try/except,
        # never silently mishandled.
        raise RuntimeError(
            f"ResumeInput refers to refund_request_id={refund_request_id!r}, which no longer exists."
        )

    _record_step(
        refund_request_id,
        STEP_TYPE_REVIEWER_DECISION,
        {"decision": reviewer_decision, "reviewer_identifier": reviewer_identifier},
        now,
    )

    if reviewer_decision != "approve":
        return Denied(refund_request_id=refund_request_id)

    lookup_succeeded, order, attempts = _resolve_order(refund_request, rate_limiter)
    _record_step(
        refund_request_id,
        STEP_TYPE_ORDER_LOOKUP,
        {
            "found": lookup_succeeded and order is not None,
            "retries_used": attempts - 1,
            "lookup_succeeded": lookup_succeeded,
        },
        now,
    )
    if not lookup_succeeded:
        return _escalated_for(refund_request)

    requested_amount_cents = _resolve_requested_amount_cents(refund_request, order)
    if order is None or requested_amount_cents is None:
        # Order genuinely not found (lookup succeeded but found nothing) or
        # no amount resolvable -- there's no policy check to fall back on
        # here (deliberately skipped, see docstring), so this can't proceed
        # to Stripe; escalate instead of raising, matching the "never
        # auto-denies, never Failed" contract for a resume.
        return _escalated_for(refund_request)

    stripe_result = _call_stripe(refund_request, order, requested_amount_cents, rate_limiter)
    if isinstance(stripe_result, Completed):
        stripe_step_data: Dict[str, Any] = {
            "outcome": "completed",
            "refund_id": stripe_result.refund_id,
            "amount_cents": stripe_result.amount_cents,
        }
    else:
        stripe_step_data = {"outcome": "escalated"}
    _record_step(refund_request_id, STEP_TYPE_STRIPE_REFUND, stripe_step_data, now)

    return stripe_result


def run(input: RunInput, rate_limiter, now: Optional[datetime] = None) -> AgentResult:
    """The Agent Loop's single entry point. `RunInput` covers both forward
    execution (NewRequestInput) and resuming after Escalation (ResumeInput)
    through the same entry point -- never a second method. `run()` itself
    still persists nothing -- the caller writes RefundRequest.status after
    run() returns (chat.py for NewRequestInput, routes/approvals.py for
    ResumeInput). For a resume specifically, RefundRequest.status is
    actually touched twice around this call, both times by the caller:
    routes/approvals.py calls db.record_reviewer_decision() *before* run()
    ever runs, which writes a short-lived interim status
    (STATUS_APPROVED/STATUS_DENIED) atomically alongside the
    ApprovalQueueEntry insert; the caller then overwrites that with the
    real resolved outcome after run() returns, exactly like the
    NewRequestInput path. run() itself is untouched by either write.

    Any unexpected failure (a policy bug, a defensive assertion, ...) is
    caught here and surfaces as Failed rather than propagating -- run()
    never raises for a resolvable RunInput. Order Lookup and Stripe
    failures are handled by their own tool-specific try/except blocks
    inside the resolution path above and never reach here as exceptions;
    only a genuine bug does.
    """
    if isinstance(input, ResumeInput):
        refund_request_id = input.refund_request_id
    elif isinstance(input, NewRequestInput):
        refund_request_id = input.refund_request.id
    else:
        raise TypeError(f"Unsupported RunInput variant: {type(input)!r}")

    current_time = now or datetime.now(timezone.utc)

    try:
        if isinstance(input, ResumeInput):
            result: AgentResult = _resume_request(
                input.refund_request_id,
                input.reviewer_decision,
                input.reviewer_identifier,
                rate_limiter,
                current_time,
            )
        else:
            result = _run_new_request(input.refund_request, rate_limiter, current_time)
    except Exception as exc:  # noqa: BLE001 -- translate any unexpected failure to Failed
        # Never the raw exception text -- the trajectory's terminal
        # `outcome` step records only the resolved status, matching the
        # "no raw exceptions" field allowlist.
        result = Failed(reason=str(exc))

    # Recorded before every return -- Completed/Escalated/Failed/Denied
    # alike. The whole step (computing the status string via _status_for
    # AND the _record_step call) is wrapped here, not just _record_step's
    # own internal guard: _status_for raises TypeError for an AgentResult
    # variant it doesn't recognize, which -- unreachable today, but a live
    # landmine for a future variant -- must never propagate out of run()
    # either. Nothing in outcome recording may affect the resolved result.
    try:
        _record_step(refund_request_id, STEP_TYPE_OUTCOME, {"status": _status_for(result)}, current_time)
    except Exception:  # noqa: BLE001 -- outcome recording must never affect the resolved AgentResult
        logger.warning(
            "Failed to record terminal outcome TrajectoryEvent for refund_request_id=%r",
            refund_request_id,
            exc_info=True,
        )
    return result
