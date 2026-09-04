"""FastAPI endpoint for chat-based refund submission.

Synchronous end-to-end: one HTTP request drives intake -> dedup -> Agent
Loop resolution -> response, no held-open connection or async resume path
yet.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import db
import rate_limit
from services import agent_loop, intake
from models import (
    STATUS_COMPLETED,
    STATUS_ESCALATED,
    STATUS_FAILED,
    ClarificationNeeded,
    Completed,
    Escalated,
    Failed,
    NewRequestInput,
    RefundRequest,
)

router = APIRouter()


# --------------------------------------------------------------------------
# DTOs: distinct types from the internal RefundRequest -- no endpoint
# returns that object directly.
# --------------------------------------------------------------------------


class ChatSubmissionRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class RefundRequestSummary(BaseModel):
    """Explicit field allowlist for what a chat client is allowed to see."""

    id: str
    order_reference: str
    reason: str
    amount_cents: Optional[int]
    status: str


class ChatSubmissionResponse(BaseModel):
    type: Literal["confirmation", "clarification", "escalated"]
    message: str
    refund_request: Optional[RefundRequestSummary] = None
    deduplicated: Optional[bool] = None


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _format_cents_as_dollars(amount_cents: int) -> str:
    """Exact-cents money formatting: integer division/modulo, never
    `amount_cents / 100` float division, which can misrender cents for
    some values."""
    dollars, cents = divmod(amount_cents, 100)
    return f"{dollars}.{cents:02d}"


def _summary_for(
    refund_request: RefundRequest,
    status: str,
    amount_cents: Optional[int] = None,
) -> RefundRequestSummary:
    """Builds the response DTO reflecting the just-resolved status.
    `amount_cents` defaults to the RefundRequest's own (possibly-null)
    value, but callers resolving a Completed outcome must pass
    `agent_result.amount_cents` explicitly -- when the customer never
    stated an amount, it resolves to the order's amount, and the response
    must reflect what was actually refunded."""
    return RefundRequestSummary(
        id=refund_request.id,
        order_reference=refund_request.order_reference,
        reason=refund_request.reason,
        amount_cents=amount_cents if amount_cents is not None else refund_request.amount_cents,
        status=status,
    )


def _rate_limit_error() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "error": {
                "code": "rate_limited",
                "message": "Too many refund chat submissions. Please try again shortly.",
                "details": None,
            }
        },
    )


@router.post("/api/chat/refund-requests", response_model=ChatSubmissionResponse)
def submit_refund_chat_message(payload: ChatSubmissionRequest, request: Request) -> ChatSubmissionResponse:
    if not rate_limit.chat_rate_limiter.allow(_client_key(request)):
        raise _rate_limit_error()

    result = intake.submit_chat_message(payload.message)

    if isinstance(result, ClarificationNeeded):
        return ChatSubmissionResponse(type="clarification", message=result.message)

    refund_request = result.refund_request

    if not result.created:
        # A dedup-reused row was already resolved by whichever submission
        # created it -- the Agent Loop must never run a second time over
        # the same RefundRequest, or a duplicate chat submission could
        # re-issue a real Stripe refund. Report its current (already
        # resolved) status as-is.
        return ChatSubmissionResponse(
            type="confirmation",
            message=(
                f"You already have a matching refund request open for order "
                f"{refund_request.order_reference}; no new request was created."
            ),
            refund_request=_summary_for(refund_request, refund_request.status),
            deduplicated=True,
        )

    agent_result = agent_loop.run(
        NewRequestInput(refund_request=refund_request),
        rate_limiter=rate_limit.tool_call_rate_limiter,
    )

    if isinstance(agent_result, Completed):
        db.update_refund_status(refund_request.id, STATUS_COMPLETED)
        return ChatSubmissionResponse(
            type="confirmation",
            message=(
                f"Good news -- your refund for order {refund_request.order_reference} "
                f"has been approved and processed for "
                f"${_format_cents_as_dollars(agent_result.amount_cents)}."
            ),
            refund_request=_summary_for(
                refund_request, STATUS_COMPLETED, amount_cents=agent_result.amount_cents
            ),
            deduplicated=False,
        )

    if isinstance(agent_result, Escalated):
        db.update_refund_status(refund_request.id, STATUS_ESCALATED)
        return ChatSubmissionResponse(
            type="escalated",
            message=(
                f"Thanks -- I've recorded your refund request for order "
                f"{refund_request.order_reference}. It needs a closer look from our "
                f"team before it can be approved; we'll follow up once it's been "
                f"reviewed."
            ),
            refund_request=_summary_for(refund_request, STATUS_ESCALATED),
            deduplicated=False,
        )

    if isinstance(agent_result, Failed):
        db.update_refund_status(refund_request.id, STATUS_FAILED)
        raise agent_loop.AgentLoopFailedError(agent_result.reason)

    # Exhaustiveness guard, not a normal-path branch: a bare `assert` here
    # would be stripped under python -O/PYTHONOPTIMIZE, letting an
    # unhandled future AgentResult variant silently fall through to an
    # implicit `None` return instead of erroring clearly.
    raise TypeError(f"Unhandled AgentResult variant: {type(agent_result)!r}")
