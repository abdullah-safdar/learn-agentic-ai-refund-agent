"""Debug endpoint for inspecting a RefundRequest's reasoning trajectory
(AD-5) -- read-only, unauthenticated (matches this project's "no auth" MVP
scope). Frontend view is deferred (see deferred-work.md); this endpoint
alone is what spec-1-4's acceptance criteria require, curl-testable.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db

router = APIRouter()


class TrajectoryEventSummary(BaseModel):
    """Explicit field allowlist for what this endpoint exposes -- never
    `TrajectoryEvent` directly (AD-12). `step_data` itself was already
    redacted to its step type's allowlist before it was ever written
    (agent_loop.py), so it's safe to pass through unshaped here."""

    sequence_no: int
    step_type: str
    step_data: Dict[str, Any]
    created_at: str


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


@router.get(
    "/api/refund-requests/{refund_request_id}/trajectory",
    response_model=List[TrajectoryEventSummary],
)
def get_refund_request_trajectory(refund_request_id: str) -> List[TrajectoryEventSummary]:
    """404 if the RefundRequest doesn't exist; an empty list (200) if it
    exists but hasn't recorded any TrajectoryEvent rows yet. Rows are
    always returned ordered by `sequence_no` -- `list_trajectory_events`
    already orders them; this never re-sorts by `created_at`."""
    refund_request = db.find_refund_request_by_id(refund_request_id)
    if refund_request is None:
        raise _not_found(refund_request_id)

    events = db.list_trajectory_events(refund_request_id)
    return [
        TrajectoryEventSummary(
            sequence_no=event.sequence_no,
            step_type=event.step_type,
            step_data=event.step_data,
            created_at=event.created_at,
        )
        for event in events
    ]
