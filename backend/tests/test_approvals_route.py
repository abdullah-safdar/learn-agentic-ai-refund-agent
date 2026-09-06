"""Endpoint-level tests for the unauthenticated staff Approval Queue
(spec-1-6): `GET /api/approvals/refund-requests` and
`POST /api/approvals/refund-requests/{id}/decision`.

Exercises the routes via FastAPI's TestClient against a fresh app with
`db.*`/`agent_loop.run` monkeypatched -- no real Postgres/Stripe calls.
agent_loop.run()'s own resume-resolution logic (order lookup, Stripe,
trajectory recording) is covered by tests/test_agent_loop.py; these tests
only cover the route layer's own concerns: request validation (400),
404/409, and wiring the AgentResult into the right HTTP response + the
right RefundRequest.status write, mirroring tests/test_trajectory_route.py's
structure and tests/test_intake.py's route-level AgentResult-to-HTTP
shaping precedent (chat.py).
"""

from __future__ import annotations

from typing import List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db
from errors import register_error_handlers
from models import Completed, Denied, Escalated, Failed, RefundRequest, ResumeInput
from routes import approvals as approvals_api
from services import agent_loop

FIXED_NOW_ISO = "2026-09-05T12:00:00.000Z"


def make_refund_request(
    refund_request_id: str = "rr-1",
    order_reference: str = "ORD-1234",
    amount_cents: Optional[int] = 1000,
    status: str = "escalated",
) -> RefundRequest:
    return RefundRequest(
        id=refund_request_id,
        order_reference=order_reference,
        reason="wrong size",
        amount_cents=amount_cents,
        status=status,
        created_at=FIXED_NOW_ISO,
    )


def _build_test_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    escalated_requests: Optional[List[RefundRequest]] = None,
    find_by_id_result: Optional[RefundRequest] = None,
    record_decision_side_effect=None,
    agent_result=None,
) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(approvals_api.router)

    monkeypatch.setattr(db, "list_escalated_refund_requests", lambda: escalated_requests or [])
    monkeypatch.setattr(db, "find_refund_request_by_id", lambda rrid: find_by_id_result)
    monkeypatch.setattr(db, "update_refund_status", lambda rrid, status: None)

    def fake_record_reviewer_decision(rrid, decision, reviewer_identifier, now):
        if record_decision_side_effect is not None:
            raise record_decision_side_effect
        return None

    monkeypatch.setattr(db, "record_reviewer_decision", fake_record_reviewer_decision)

    if agent_result is not None:
        monkeypatch.setattr(agent_loop, "run", lambda run_input, rate_limiter, now=None: agent_result)

    return app


# --------------------------------------------------------------------------
# GET /api/approvals/refund-requests
# --------------------------------------------------------------------------


def test_list_returns_only_escalated_requests_as_allowlisted_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    escalated = [make_refund_request("rr-1"), make_refund_request("rr-2", order_reference="ORD-5678")]
    app = _build_test_app(monkeypatch, escalated_requests=escalated)
    client = TestClient(app)

    response = client.get("/api/approvals/refund-requests")

    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body] == ["rr-1", "rr-2"]
    for row in body:
        assert set(row.keys()) == {"id", "order_reference", "reason", "amount_cents", "status", "created_at"}
        assert row["status"] == "escalated"


def test_list_returns_empty_list_when_no_requests_are_escalated(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_test_app(monkeypatch, escalated_requests=[])
    client = TestClient(app)

    response = client.get("/api/approvals/refund-requests")

    assert response.status_code == 200
    assert response.json() == []


# --------------------------------------------------------------------------
# POST /api/approvals/refund-requests/{id}/decision -- happy paths
# --------------------------------------------------------------------------


def test_approve_completes_records_decision_calls_run_and_persists_completed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refund_request = make_refund_request("rr-approve")
    recorded_calls = []
    status_writes = []

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(approvals_api.router)
    monkeypatch.setattr(db, "find_refund_request_by_id", lambda rrid: refund_request)
    monkeypatch.setattr(
        db,
        "record_reviewer_decision",
        lambda rrid, decision, reviewer_identifier, now: recorded_calls.append((rrid, decision, reviewer_identifier)),
    )
    monkeypatch.setattr(db, "update_refund_status", lambda rrid, status: status_writes.append((rrid, status)))

    captured_run_input = {}

    def fake_run(run_input, rate_limiter, now=None):
        captured_run_input["value"] = run_input
        return Completed(refund_id="re_via_route", amount_cents=1000)

    monkeypatch.setattr(agent_loop, "run", fake_run)
    client = TestClient(app)

    response = client.post(
        "/api/approvals/refund-requests/rr-approve/decision",
        json={"decision": "approve", "reviewer_identifier": "staff@example.com"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["refund_request"]["status"] == "completed"
    assert body["refund_request"]["amount_cents"] == 1000
    assert recorded_calls == [("rr-approve", "approve", "staff@example.com")]
    assert status_writes == [("rr-approve", "completed")]
    run_input = captured_run_input["value"]
    assert isinstance(run_input, ResumeInput)
    assert run_input.refund_request_id == "rr-approve"
    assert run_input.reviewer_decision == "approve"
    assert run_input.reviewer_identifier == "staff@example.com"


def test_approve_re_escalated_persists_escalated_status(monkeypatch: pytest.MonkeyPatch) -> None:
    refund_request = make_refund_request("rr-re-escalate")
    app = _build_test_app(
        monkeypatch,
        find_by_id_result=refund_request,
        agent_result=Escalated(pending_review_id="rr-re-escalate", tentative_recommendation="Needs manual review"),
    )
    client = TestClient(app)

    response = client.post(
        "/api/approvals/refund-requests/rr-re-escalate/decision",
        json={"decision": "approve", "reviewer_identifier": "staff@example.com"},
    )

    assert response.status_code == 200
    assert response.json()["refund_request"]["status"] == "escalated"


def test_deny_persists_denied_status_and_never_calls_stripe_side(monkeypatch: pytest.MonkeyPatch) -> None:
    refund_request = make_refund_request("rr-deny")
    app = _build_test_app(
        monkeypatch,
        find_by_id_result=refund_request,
        agent_result=Denied(refund_request_id="rr-deny"),
    )
    client = TestClient(app)

    response = client.post(
        "/api/approvals/refund-requests/rr-deny/decision",
        json={"decision": "deny", "reviewer_identifier": "staff@example.com"},
    )

    assert response.status_code == 200
    assert response.json()["refund_request"]["status"] == "denied"


def test_failed_agent_result_returns_502_resolution_failed_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected Agent Loop failure (AgentResult.Failed) on resume
    surfaces as a 502 resolution_failed envelope, mirroring
    test_intake.py's identical precedent for the NewRequestInput path
    (chat.py). The RefundRequest row must be persisted as "failed" *before*
    the 502 is raised (routes/approvals.py calls update_refund_status()
    before raising AgentLoopFailedError) -- confirm that actually happened,
    not just that the HTTP envelope looks right."""
    refund_request = make_refund_request("rr-failed")
    status_writes = []

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(approvals_api.router)
    monkeypatch.setattr(db, "find_refund_request_by_id", lambda rrid: refund_request)
    monkeypatch.setattr(db, "record_reviewer_decision", lambda rrid, decision, reviewer_identifier, now: None)
    monkeypatch.setattr(db, "update_refund_status", lambda rrid, status: status_writes.append((rrid, status)))
    monkeypatch.setattr(agent_loop, "run", lambda run_input, rate_limiter, now=None: Failed(reason="simulated failure"))
    client = TestClient(app)

    response = client.post(
        "/api/approvals/refund-requests/rr-failed/decision",
        json={"decision": "approve", "reviewer_identifier": "staff@example.com"},
    )

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "resolution_failed"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert status_writes == [("rr-failed", "failed")]


# --------------------------------------------------------------------------
# Error paths -- I/O & Edge-Case Matrix
# --------------------------------------------------------------------------


def test_unknown_refund_request_id_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_test_app(monkeypatch, find_by_id_result=None)
    client = TestClient(app)

    response = client.post(
        "/api/approvals/refund-requests/does-not-exist/decision",
        json={"decision": "approve", "reviewer_identifier": "staff@example.com"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "refund_request_not_found"


def test_concurrent_decision_returns_409_and_never_calls_run(monkeypatch: pytest.MonkeyPatch) -> None:
    refund_request = make_refund_request("rr-race")
    run_calls = []
    app = _build_test_app(
        monkeypatch,
        find_by_id_result=refund_request,
        record_decision_side_effect=db.ConcurrentDecisionError("already resolved"),
    )
    monkeypatch.setattr(agent_loop, "run", lambda *args, **kwargs: run_calls.append(1))
    client = TestClient(app)

    response = client.post(
        "/api/approvals/refund-requests/rr-race/decision",
        json={"decision": "approve", "reviewer_identifier": "staff@example.com"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "refund_request_not_escalated"
    assert run_calls == []


def test_invalid_decision_value_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    refund_request = make_refund_request("rr-bad-decision")
    app = _build_test_app(monkeypatch, find_by_id_result=refund_request)
    client = TestClient(app)

    response = client.post(
        "/api/approvals/refund-requests/rr-bad-decision/decision",
        json={"decision": "maybe", "reviewer_identifier": "staff@example.com"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_decision_request"


def test_blank_reviewer_identifier_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    refund_request = make_refund_request("rr-blank-reviewer")
    app = _build_test_app(monkeypatch, find_by_id_result=refund_request)
    client = TestClient(app)

    response = client.post(
        "/api/approvals/refund-requests/rr-blank-reviewer/decision",
        json={"decision": "deny", "reviewer_identifier": "   "},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_decision_request"


def test_invalid_input_checked_before_refund_request_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """400 for a malformed decision even when the id wouldn't resolve to a
    real row either -- the request-shape check runs first."""
    lookups = []
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(approvals_api.router)
    monkeypatch.setattr(db, "find_refund_request_by_id", lambda rrid: lookups.append(rrid))
    client = TestClient(app)

    response = client.post(
        "/api/approvals/refund-requests/does-not-exist/decision",
        json={"decision": "not-a-real-decision", "reviewer_identifier": "staff@example.com"},
    )

    assert response.status_code == 400
    assert lookups == []
