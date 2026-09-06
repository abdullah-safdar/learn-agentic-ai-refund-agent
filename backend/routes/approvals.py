"""FastAPI endpoints for the unauthenticated staff Approval Queue (AD-7,
spec-1-6): list Escalated RefundRequests, and record a reviewer's
approve/deny decision -- then resume the Agent Loop to completion via the
same `run(ResumeInput(...))` entry point chat.py's NewRequestInput path
already uses.

Mirrors chat.py's isinstance-chain-over-AgentResult response shaping and
trajectory.py's 404 style.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db
import rate_limit
from services import agent_loop
from models import (
    STATUS_COMPLETED,
    STATUS_DENIED,
    STATUS_ESCALATED,
    STATUS_FAILED,
    Completed,
    Denied,
    Escalated,
    Failed,
    RefundRequest,
    ResumeInput,
)

router = APIRouter()


# --------------------------------------------------------------------------
# DTOs: distinct types from the internal RefundRequest -- no endpoint
# returns that object directly (mirrors chat.py/trajectory.py).
# --------------------------------------------------------------------------


class RefundRequestSummary(BaseModel):
    """Explicit field allowlist for what an unauthenticated staff client is
    allowed to see."""

    id: str
    order_reference: str
    reason: str
    amount_cents: Optional[int]
    status: str
    created_at: str


class DecisionRequest(BaseModel):
    """Deliberately permissive field types -- `decision`/`reviewer_identifier`
    are validated explicitly below (raising 400) rather than via Pydantic's
    own type/constraint validation (which this app's global handler turns
    into 422), matching the I/O & Edge-Case Matrix's "400" for invalid
    input."""

    decision: str
    reviewer_identifier: str


class DecisionResponse(BaseModel):
    refund_request: RefundRequestSummary


def _summary_for(
    refund_request: RefundRequest,
    status: str,
    amount_cents: Optional[int] = None,
) -> RefundRequestSummary:
    return RefundRequestSummary(
        id=refund_request.id,
        order_reference=refund_request.order_reference,
        reason=refund_request.reason,
        amount_cents=amount_cents if amount_cents is not None else refund_request.amount_cents,
        status=status,
        created_at=refund_request.created_at,
    )


def _bad_request(message: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"error": {"code": "invalid_decision_request", "message": message, "details": None}},
    )


def _not_found(refund_request_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": {
                "code": "refund_request_not_found",
                "message": f"No refund request found with id {refund_request_id!r}.",
                "details": None,
            }
        },
    )


def _conflict(refund_request_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "code": "refund_request_not_escalated",
                "message": (
                    f"Refund request {refund_request_id!r} is no longer awaiting review -- "
                    "a decision was already recorded for it."
                ),
                "details": None,
            }
        },
    )


@router.get("/api/approvals/refund-requests", response_model=List[RefundRequestSummary])
def list_escalated_refund_requests() -> List[RefundRequestSummary]:
    """Only RefundRequests currently `status = "escalated"` -- resolved via
    db.list_escalated_refund_requests(), never via ApprovalQueueEntry (AD-7:
    RefundRequest.status stays the sole canonical lifecycle field)."""
    return [
        _summary_for(refund_request, refund_request.status)
        for refund_request in db.list_escalated_refund_requests()
    ]


@router.post(
    "/api/approvals/refund-requests/{refund_request_id}/decision",
    response_model=DecisionResponse,
)
def decide_refund_request(refund_request_id: str, payload: DecisionRequest) -> DecisionResponse:
    if payload.decision not in ("approve", "deny"):
        raise _bad_request('decision must be "approve" or "deny".')
    if not payload.reviewer_identifier.strip():
        raise _bad_request("reviewer_identifier must not be blank.")

    refund_request = db.find_refund_request_by_id(refund_request_id)
    if refund_request is None:
        raise _not_found(refund_request_id)

    try:
        db.record_reviewer_decision(
            refund_request_id, payload.decision, payload.reviewer_identifier, datetime.now(timezone.utc)
        )
    except db.ConcurrentDecisionError:
        raise _conflict(refund_request_id)

    agent_result = agent_loop.run(
        ResumeInput(
            refund_request_id=refund_request_id,
            reviewer_decision=payload.decision,
            reviewer_identifier=payload.reviewer_identifier,
        ),
        rate_limiter=rate_limit.tool_call_rate_limiter,
    )

    if isinstance(agent_result, Completed):
        db.update_refund_status(refund_request_id, STATUS_COMPLETED)
        resolved_status, amount_cents = STATUS_COMPLETED, agent_result.amount_cents
    elif isinstance(agent_result, Escalated):
        db.update_refund_status(refund_request_id, STATUS_ESCALATED)
        resolved_status, amount_cents = STATUS_ESCALATED, None
    elif isinstance(agent_result, Denied):
        db.update_refund_status(refund_request_id, STATUS_DENIED)
        resolved_status, amount_cents = STATUS_DENIED, None
    elif isinstance(agent_result, Failed):
        db.update_refund_status(refund_request_id, STATUS_FAILED)
        raise agent_loop.AgentLoopFailedError(agent_result.reason)
    else:
        # Exhaustiveness guard, not a normal-path branch -- see chat.py's
        # identical guard for why this isn't a bare `assert`.
        raise TypeError(f"Unhandled AgentResult variant: {type(agent_result)!r}")

    refreshed = db.find_refund_request_by_id(refund_request_id) or refund_request
    return DecisionResponse(refund_request=_summary_for(refreshed, resolved_status, amount_cents=amount_cents))
