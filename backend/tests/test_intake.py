"""Unit tests for the I/O & Edge-Case Matrix in
spec-1-1-submit-a-refund-request-via-chat.md, plus regression coverage added
after the Story 1.1 review pass (dedup-race handling, dedup-key robustness,
extraction-failure handling). Also covers Story 1.2's API-level shaping:
the `Completed` response shape and the `Failed` -> 502 envelope (the I/O
matrix's own scenarios live in tests/test_agent_loop.py; these two just
confirm the chat endpoint wires `agent_loop.run()`'s result into the right
HTTP response).

Domain-level scenarios (happy path, duplicate, missing order reference,
dedup-race, extraction failure) call `intake.submit_chat_message` directly
with `llm.extract_refund_request`/`db.find_refund_request_by_dedup_key`/
`db.save_refund_request` monkeypatched -- Python's version of
`jest.spyOn(module, 'fn').mockImplementation(...)`. No real Anthropic or
Postgres calls. The rate-limit, extraction-failure-envelope, and Story 1.2
scenarios exercise the API layer (rate limiting is enforced there, and
error envelopes are the API layer's translation concern), via FastAPI's
TestClient against a fresh app with the same functions monkeypatched.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db
import rate_limit
from errors import register_error_handlers
from routes import chat as chat_api
from services import intake, llm, policy, stripe_refund
from models import (
    ORDER_STATUS_COMPLETED,
    ClarificationNeeded,
    ExtractedRefundFields,
    IntakeResult,
    Order,
    RefundRequest,
)
from rate_limit import InMemoryRateLimiter

UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
ISO8601_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

FIXED_NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fakes + monkeypatch helpers
# --------------------------------------------------------------------------


class FakeRepo:
    """In-memory stand-in for db.py's refund-request functions -- keyed
    purely by the dedup key the domain hands it, exactly like the real
    Postgres functions' contract."""

    def __init__(self) -> None:
        self.by_dedup_key: Dict[str, RefundRequest] = {}
        self.statuses_by_id: Dict[str, str] = {}

    def find(self, dedup_key: str) -> Optional[RefundRequest]:
        return self.by_dedup_key.get(dedup_key)

    def save(self, refund_request: RefundRequest, dedup_key: str) -> bool:
        if dedup_key in self.by_dedup_key:
            return False
        self.by_dedup_key[dedup_key] = refund_request
        return True

    def update_status(self, refund_request_id: str, status: str) -> None:
        self.statuses_by_id[refund_request_id] = status


class RaceyFakeRepo(FakeRepo):
    """Simulates losing a concurrent insert race: find() reports nothing
    exists (matching what the domain saw when it checked), but by the time
    save() is called, some other submission has already been persisted
    under the same dedup_key -- exactly like a real database
    UNIQUE-constraint race resolved by `ON CONFLICT DO NOTHING`."""

    def __init__(self, concurrent_winner: RefundRequest) -> None:
        super().__init__()
        self._concurrent_winner = concurrent_winner

    def save(self, refund_request: RefundRequest, dedup_key: str) -> bool:
        # The "concurrent" winner lands in storage right as our save() is
        # attempted -- our own row never gets persisted.
        self.by_dedup_key[dedup_key] = self._concurrent_winner
        return False


class FakeOrderLookup:
    def __init__(self, order: Optional[Order] = None) -> None:
        self._order = order

    def __call__(self, order_reference: str) -> Optional[Order]:
        return self._order


class FakeStripe:
    def __init__(self, refund_id: str = "re_test123", should_raise: bool = False) -> None:
        self._refund_id = refund_id
        self._should_raise = should_raise
        self.calls = 0

    def __call__(self, order: Order, amount_cents: int, reason: str) -> str:
        self.calls += 1
        if self._should_raise:
            raise RuntimeError("simulated stripe failure")
        return self._refund_id


def raising_policy(order, requested_amount_cents, now):
    """Simulates a policy implementation that violates its own contract by
    raising instead of always returning a PolicyDecision -- the only way
    agent_loop.run() resolves to Failed in this file (see
    tests/test_agent_loop.py's own unit-level version)."""
    raise RuntimeError("simulated policy failure")


def install_repo(monkeypatch: pytest.MonkeyPatch, repo: FakeRepo) -> None:
    monkeypatch.setattr(db, "find_refund_request_by_dedup_key", repo.find)
    monkeypatch.setattr(db, "save_refund_request", repo.save)
    monkeypatch.setattr(db, "update_refund_status", repo.update_status)


def install_llm(monkeypatch: pytest.MonkeyPatch, result: ExtractedRefundFields) -> None:
    monkeypatch.setattr(llm, "extract_refund_request", lambda chat_text: result)


def install_raising_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(chat_text: str) -> ExtractedRefundFields:
        raise RuntimeError("simulated provider failure")

    monkeypatch.setattr(llm, "extract_refund_request", _raise)


# --------------------------------------------------------------------------
# Domain-level tests (intake.submit_chat_message against monkeypatched
# llm/db functions)
# --------------------------------------------------------------------------


def test_happy_path_creates_refund_request(monkeypatch: pytest.MonkeyPatch) -> None:
    install_llm(monkeypatch, ExtractedRefundFields(order_reference="ORD-1234", reason="wrong size", amount_cents=None))
    repo = FakeRepo()
    install_repo(monkeypatch, repo)

    result = intake.submit_chat_message("Refund order #ORD-1234, wrong size", now=FIXED_NOW)

    assert isinstance(result, IntakeResult)
    assert result.created is True
    rr = result.refund_request
    assert rr.order_reference == "ORD-1234"
    assert rr.reason == "wrong size"
    assert rr.amount_cents is None
    assert UUID_V4_RE.match(rr.id), f"id is not a UUIDv4 string: {rr.id!r}"
    assert uuid.UUID(rr.id).version == 4
    assert ISO8601_UTC_RE.match(rr.created_at), f"created_at is not ISO-8601 UTC: {rr.created_at!r}"
    assert len(repo.by_dedup_key) == 1


def test_duplicate_submission_reuses_existing_row_despite_reason_case_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises normalize_reason's actual normalization -- if it were
    silently removed, "wrong size" vs "  Wrong   Size  " would no longer
    dedup and this test would fail."""
    repo = FakeRepo()
    install_repo(monkeypatch, repo)

    install_llm(monkeypatch, ExtractedRefundFields(order_reference="ORD-1234", reason="wrong size", amount_cents=None))
    first = intake.submit_chat_message("Refund order #ORD-1234, wrong size", now=FIXED_NOW)

    install_llm(
        monkeypatch, ExtractedRefundFields(order_reference="ORD-1234", reason="  Wrong   Size  ", amount_cents=None)
    )
    second = intake.submit_chat_message("Refund order #ORD-1234, WRONG SIZE again please", now=FIXED_NOW)

    assert isinstance(first, IntakeResult) and first.created is True
    assert isinstance(second, IntakeResult) and second.created is False
    assert second.refund_request.id == first.refund_request.id
    assert len(repo.by_dedup_key) == 1  # no duplicate row was created


def test_duplicate_submission_dedups_across_order_reference_casing(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakeRepo()
    install_repo(monkeypatch, repo)

    install_llm(monkeypatch, ExtractedRefundFields(order_reference="ORD-1234", reason="wrong size", amount_cents=None))
    first = intake.submit_chat_message("Refund order #ORD-1234, wrong size", now=FIXED_NOW)

    install_llm(monkeypatch, ExtractedRefundFields(order_reference="ord-1234", reason="wrong size", amount_cents=None))
    second = intake.submit_chat_message("refund order ord-1234, wrong size", now=FIXED_NOW)

    assert first.created is True
    assert second.created is False
    assert second.refund_request.id == first.refund_request.id
    # The stored/displayed order_reference is never rewritten by the key
    # normalization -- it keeps whatever the *winning* (first) row had.
    assert second.refund_request.order_reference == "ORD-1234"


def test_missing_order_reference_asks_for_clarification(monkeypatch: pytest.MonkeyPatch) -> None:
    install_llm(monkeypatch, ExtractedRefundFields(order_reference="", reason="wants a refund", amount_cents=None))
    repo = FakeRepo()
    install_repo(monkeypatch, repo)

    result = intake.submit_chat_message("I want a refund", now=FIXED_NOW)

    assert isinstance(result, ClarificationNeeded)
    assert result.message  # non-empty clarification prompt returned, not a crash
    assert len(repo.by_dedup_key) == 0  # no RefundRequest row created


def test_dedup_key_collision_on_save_returns_persisted_row_not_local_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """If save() reports it lost the insert race (a concurrent submission
    won), submit_chat_message must return the row that actually got
    persisted -- never the locally-constructed RefundRequest that was
    silently discarded."""
    install_llm(monkeypatch, ExtractedRefundFields(order_reference="ORD-7777", reason="damaged", amount_cents=None))
    concurrent_winner = RefundRequest.new(order_reference="ORD-7777", reason="damaged", amount_cents=500)
    repo = RaceyFakeRepo(concurrent_winner)
    install_repo(monkeypatch, repo)

    result = intake.submit_chat_message("Refund order #ORD-7777, damaged", now=FIXED_NOW)

    assert isinstance(result, IntakeResult)
    assert result.created is False
    assert result.refund_request.id == concurrent_winner.id
    assert result.refund_request.amount_cents == 500


def test_compute_dedup_key_rejects_naive_datetime() -> None:
    naive_now = datetime(2026, 8, 27, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError):
        intake.compute_dedup_key("ORD-1234", "wrong size", naive_now)


def test_dedup_key_does_not_collide_across_pipe_delimiter_boundaries() -> None:
    """Naive '|'-joined concatenation would make these two different
    (order_reference, reason) pairs serialize to the same string and
    therefore hash to the same key. They must not collide."""
    key_a = intake.compute_dedup_key("ORD-1|extra", "foo", FIXED_NOW)
    key_b = intake.compute_dedup_key("ORD-1", "extra|foo", FIXED_NOW)
    assert key_a != key_b


def test_llm_extraction_failure_raises_extraction_error(monkeypatch: pytest.MonkeyPatch) -> None:
    install_raising_llm(monkeypatch)
    repo = FakeRepo()
    install_repo(monkeypatch, repo)

    with pytest.raises(intake.ExtractionError):
        intake.submit_chat_message("Refund order #ORD-1234, wrong size", now=FIXED_NOW)

    assert len(repo.by_dedup_key) == 0  # nothing was persisted on failure


# --------------------------------------------------------------------------
# API-level tests (TestClient against a fresh app, same functions
# monkeypatched, plus test-sized rate limiters)
# --------------------------------------------------------------------------


def _build_test_app(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rate_limit_count: int = 100,
    tool_call_rate_limit_count: int = 1000,
    llm_result: Optional[ExtractedRefundFields] = None,
    llm_raises: bool = False,
    repo: Optional[FakeRepo] = None,
    order: Optional[Order] = None,
    policy_fn=None,
    stripe: Optional[FakeStripe] = None,
) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(chat_api.router)

    if llm_raises:
        install_raising_llm(monkeypatch)
    else:
        install_llm(
            monkeypatch,
            llm_result or ExtractedRefundFields(order_reference="ORD-9999", reason="damaged", amount_cents=None),
        )

    install_repo(monkeypatch, repo or FakeRepo())

    # Defaults: no matching order, the real (pure-logic, no-I/O) hardcoded
    # policy function, and a Stripe fake that must never actually be called
    # given the "order not found" default above -- together these keep
    # every Story 1.1-style test passing (it resolves to Escalated, still a
    # 200 response) without needing to know about the Agent Loop at all.
    monkeypatch.setattr(db, "find_order_by_reference", FakeOrderLookup(order=order))
    monkeypatch.setattr(policy, "evaluate_policy", policy_fn or policy.evaluate_policy)
    monkeypatch.setattr(stripe_refund, "issue_refund", stripe or FakeStripe())

    # Fresh, test-sized rate limiter instances -- swapped in per test app so
    # tests never interfere with each other's or the real module-level
    # defaults' budget.
    monkeypatch.setattr(
        rate_limit, "chat_rate_limiter", InMemoryRateLimiter(limit=rate_limit_count, window_seconds=60)
    )
    monkeypatch.setattr(
        rate_limit,
        "tool_call_rate_limiter",
        InMemoryRateLimiter(limit=tool_call_rate_limit_count, window_seconds=60),
    )

    return app


def test_rate_limit_exceeded_returns_429_with_error_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_test_app(monkeypatch, rate_limit_count=1)
    client = TestClient(app)

    first = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-9999, damaged"})
    second = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-9999, damaged"})

    assert first.status_code == 200
    assert second.status_code == 429
    body = second.json()
    assert body["error"]["code"] == "rate_limited"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_extraction_failure_returns_502_with_error_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _build_test_app(monkeypatch, llm_raises=True)
    client = TestClient(app)

    response = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-1234, wrong size"})

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "extraction_failed"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_completed_response_shape_for_compliant_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compliant order flows all the way through chat_api -> agent_loop
    -> Stripe, and the response reflects the resolved outcome -- never
    "pending"."""
    order = Order(
        id="order-1",
        order_reference="ORD-2000",
        status=ORDER_STATUS_COMPLETED,
        amount_cents=2500,
        order_date=datetime(2026, 8, 20, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        stripe_payment_intent_id="pi_test123",
    )
    repo = FakeRepo()
    app = _build_test_app(
        monkeypatch,
        llm_result=ExtractedRefundFields(order_reference="ORD-2000", reason="wrong item", amount_cents=2500),
        repo=repo,
        order=order,
        stripe=FakeStripe(refund_id="re_completed_test"),
    )
    client = TestClient(app)

    response = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-2000, wrong item"})

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "confirmation"
    assert "25.00" in body["message"]
    assert body["refund_request"]["status"] == "completed"
    # The repository was actually told about the resolved outcome -- a
    # dropped or wrong update_refund_status() call wouldn't be caught by
    # the response-shape assertions above alone.
    assert repo.statuses_by_id[body["refund_request"]["id"]] == "completed"


def test_completed_response_reflects_resolved_amount_when_request_amount_was_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the customer never stated an amount, run() resolves it to the
    order's full amount -- the structured refund_request.amount_cents in
    the response must reflect that resolved amount, not stay null."""
    order = Order(
        id="order-1",
        order_reference="ORD-2100",
        status=ORDER_STATUS_COMPLETED,
        amount_cents=7500,
        order_date=datetime(2026, 8, 20, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        stripe_payment_intent_id="pi_test789",
    )
    app = _build_test_app(
        monkeypatch,
        llm_result=ExtractedRefundFields(order_reference="ORD-2100", reason="changed my mind", amount_cents=None),
        order=order,
        stripe=FakeStripe(refund_id="re_null_amount_test"),
    )
    client = TestClient(app)

    response = client.post(
        "/api/chat/refund-requests", json={"message": "Refund order #ORD-2100, changed my mind"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "confirmation"
    assert body["refund_request"]["amount_cents"] == 7500  # resolved to the order's full amount, not null
    assert "75.00" in body["message"]


def test_escalated_response_shape_for_non_compliant_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-compliant order (not found, in this case) resolves to
    Escalated -- the response contract (type, persisted status) for that
    branch."""
    app = _build_test_app(
        monkeypatch,
        llm_result=ExtractedRefundFields(order_reference="ORD-2200", reason="wrong size", amount_cents=None),
        order=None,
    )
    client = TestClient(app)

    response = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-2200, wrong size"})

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "escalated"
    assert body["refund_request"]["status"] == "escalated"


def test_failed_agent_result_returns_502_resolution_failed_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected Agent Loop failure (AgentResult.Failed) surfaces as a
    502 resolution_failed envelope, distinct from extraction_failed."""
    order = Order(
        id="order-1",
        order_reference="ORD-3000",
        status=ORDER_STATUS_COMPLETED,
        amount_cents=1000,
        order_date=datetime(2026, 8, 20, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        stripe_payment_intent_id="pi_test456",
    )
    repo = FakeRepo()
    app = _build_test_app(
        monkeypatch,
        llm_result=ExtractedRefundFields(order_reference="ORD-3000", reason="damaged", amount_cents=1000),
        repo=repo,
        order=order,
        policy_fn=raising_policy,
    )
    client = TestClient(app)

    response = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-3000, damaged"})

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "resolution_failed"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    # The RefundRequest row was persisted as "failed" *before* the 502 was
    # raised (chat_api.py calls update_refund_status() before raising
    # AgentLoopFailedError) -- confirm that actually happened, not just
    # that the HTTP envelope looks right.
    request_id = next(iter(repo.statuses_by_id))
    assert repo.statuses_by_id[request_id] == "failed"


def test_deduplicated_request_does_not_re_run_agent_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicate chat submission within the dedup window must never run
    the Agent Loop a second time over the same RefundRequest -- doing so
    could re-issue a real Stripe refund. Posting the same message twice
    should resolve the request once (Stripe called once) and the second
    response should report deduplicated: true without any further Stripe
    calls."""
    order = Order(
        id="order-1",
        order_reference="ORD-2300",
        status=ORDER_STATUS_COMPLETED,
        amount_cents=1500,
        order_date=datetime(2026, 8, 20, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        stripe_payment_intent_id="pi_test999",
    )
    stripe = FakeStripe(refund_id="re_dedup_test")
    app = _build_test_app(
        monkeypatch,
        llm_result=ExtractedRefundFields(order_reference="ORD-2300", reason="wrong color", amount_cents=1500),
        order=order,
        stripe=stripe,
    )
    client = TestClient(app)

    first = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-2300, wrong color"})
    assert first.status_code == 200
    assert first.json()["type"] == "confirmation"
    assert first.json()["refund_request"]["status"] == "completed"
    calls_after_first = stripe.calls
    assert calls_after_first == 1

    second = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-2300, wrong color"})

    assert second.status_code == 200
    body = second.json()
    assert body["deduplicated"] is True
    assert stripe.calls == calls_after_first  # the Agent Loop did not run again
