"""Endpoint-level tests for the debug trajectory route in
spec-1-4-inspect-the-agents-reasoning.md:
`GET /api/refund-requests/{refund_request_id}/trajectory`.

Exercises the route via FastAPI's TestClient against a fresh app with
`db.find_refund_request_by_id`/`db.list_trajectory_events` monkeypatched --
no real Postgres calls. The trajectory-recording behavior itself (which
steps get written, with what redacted step_data) is covered by
tests/test_agent_loop.py; these tests only cover the endpoint's own
concerns: ordering, the 404-vs-empty-list distinction, and the DTO's field
allowlist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db
from errors import register_error_handlers
from models import RefundRequest, TrajectoryEvent
from routes import trajectory as trajectory_api

FIXED_NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


def make_refund_request(refund_request_id: str = "rr-1") -> RefundRequest:
    return RefundRequest(
        id=refund_request_id,
        order_reference="ORD-1234",
        reason="wrong size",
        amount_cents=1000,
        status="completed",
        created_at="2026-09-04T12:00:00.000Z",
    )


def make_event(refund_request_id: str, sequence_no: int, step_type: str, step_data: dict) -> TrajectoryEvent:
    return TrajectoryEvent(
        id=f"event-{sequence_no}",
        refund_request_id=refund_request_id,
        sequence_no=sequence_no,
        step_type=step_type,
        step_data=step_data,
        created_at="2026-09-04T12:00:00.000Z",
    )


def _build_test_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    refund_request: Optional[RefundRequest],
    events: Optional[List[TrajectoryEvent]] = None,
) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(trajectory_api.router)

    monkeypatch.setattr(db, "find_refund_request_by_id", lambda rrid: refund_request)
    monkeypatch.setattr(db, "list_trajectory_events", lambda rrid: events or [])

    return app


def test_returns_events_ordered_by_sequence_no_with_only_allowlisted_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refund_request = make_refund_request("rr-ordered")
    events = [
        make_event("rr-ordered", 1, "order_lookup", {"found": True, "retries_used": 0}),
        make_event("rr-ordered", 2, "policy_decision", {"compliant": True, "confidence": 1.0, "citation_ids": []}),
        make_event(
            "rr-ordered", 3, "stripe_refund", {"outcome": "completed", "refund_id": "re_1", "amount_cents": 1000}
        ),
        make_event("rr-ordered", 4, "outcome", {"status": "completed"}),
    ]
    app = _build_test_app(monkeypatch, refund_request=refund_request, events=events)
    client = TestClient(app)

    response = client.get("/api/refund-requests/rr-ordered/trajectory")

    assert response.status_code == 200
    body = response.json()
    assert [row["sequence_no"] for row in body] == [1, 2, 3, 4]
    assert [row["step_type"] for row in body] == [
        "order_lookup",
        "policy_decision",
        "stripe_refund",
        "outcome",
    ]
    for row in body:
        assert set(row.keys()) == {"sequence_no", "step_type", "step_data", "created_at"}
    assert body[2]["step_data"] == {"outcome": "completed", "refund_id": "re_1", "amount_cents": 1000}


def test_unknown_refund_request_id_returns_404_with_standard_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_test_app(monkeypatch, refund_request=None)
    client = TestClient(app)

    response = client.get("/api/refund-requests/does-not-exist/trajectory")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "refund_request_not_found"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_existing_request_with_no_events_yet_returns_200_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    refund_request = make_refund_request("rr-pending")
    app = _build_test_app(monkeypatch, refund_request=refund_request, events=[])
    client = TestClient(app)

    response = client.get("/api/refund-requests/rr-pending/trajectory")

    assert response.status_code == 200
    assert response.json() == []
