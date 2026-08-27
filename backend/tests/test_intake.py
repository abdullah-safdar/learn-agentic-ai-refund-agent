"""Unit tests for the I/O & Edge-Case Matrix in
spec-1-1-submit-a-refund-request-via-chat.md, plus regression coverage added
after the Story 1.1 review pass (dedup-race handling, dedup-key robustness,
extraction-failure handling).

Domain-level scenarios (happy path, duplicate, missing order reference,
dedup-race, extraction failure) call `domain.intake.submit_chat_message`
directly against fakes for LLMPort/RefundRepositoryPort -- no real Anthropic
or Postgres calls. The rate-limit and extraction-failure-envelope scenarios
exercise the API layer (rate limiting is enforced there per AD-11, and error
envelopes are the API layer's translation concern), via FastAPI's
TestClient with the same fakes injected through dependency overrides.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import chat_routes
from api.errors import register_error_handlers
from domain.intake import (
    ClarificationNeeded,
    ExtractionError,
    IntakeResult,
    compute_dedup_key,
    submit_chat_message,
)
from domain.models import RefundRequest
from domain.ports import ExtractedRefundFields, LLMPort, RefundRepositoryPort

UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
ISO8601_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

FIXED_NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


class FakeLLM(LLMPort):
    """Returns a canned extraction result regardless of input text --
    real extraction quality is the adapter's concern, not intake.py's."""

    def __init__(self, result: ExtractedRefundFields) -> None:
        self._result = result

    def extract_refund_request(self, chat_text: str) -> ExtractedRefundFields:
        return self._result


class RaisingFakeLLM(LLMPort):
    """Simulates the LLMPort failing outright (e.g. the provider call
    errors), as opposed to succeeding with empty/ambiguous fields."""

    def extract_refund_request(self, chat_text: str) -> ExtractedRefundFields:
        raise RuntimeError("simulated provider failure")


class FakeRepository(RefundRepositoryPort):
    """In-memory RefundRepositoryPort -- keyed purely by the dedup key the
    domain hands it, exactly like the real Postgres adapter's contract."""

    def __init__(self) -> None:
        self.by_dedup_key: Dict[str, RefundRequest] = {}

    def find_by_dedup_key(self, dedup_key: str) -> Optional[RefundRequest]:
        return self.by_dedup_key.get(dedup_key)

    def save(self, refund_request: RefundRequest, dedup_key: str) -> bool:
        if dedup_key in self.by_dedup_key:
            return False
        self.by_dedup_key[dedup_key] = refund_request
        return True


class RaceyFakeRepository(FakeRepository):
    """Simulates losing a concurrent insert race: `find_by_dedup_key`
    reports nothing exists (matching what the domain saw when it checked),
    but by the time `save()` is called, some other submission has already
    been persisted under the same dedup_key -- exactly like a real database
    UNIQUE-constraint race resolved by `ON CONFLICT DO NOTHING`.
    """

    def __init__(self, concurrent_winner: RefundRequest) -> None:
        super().__init__()
        self._concurrent_winner = concurrent_winner

    def save(self, refund_request: RefundRequest, dedup_key: str) -> bool:
        # The "concurrent" winner lands in storage right as our save() is
        # attempted -- our own row never gets persisted.
        self.by_dedup_key[dedup_key] = self._concurrent_winner
        return False


def test_happy_path_creates_refund_request() -> None:
    llm = FakeLLM(ExtractedRefundFields(order_reference="ORD-1234", reason="wrong size", amount_cents=None))
    repo = FakeRepository()

    result = submit_chat_message(
        "Refund order #ORD-1234, wrong size", llm=llm, repo=repo, now=FIXED_NOW
    )

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


def test_duplicate_submission_reuses_existing_row_despite_reason_case_and_whitespace() -> None:
    """Exercises normalize_reason's actual normalization -- if it were
    silently removed, "wrong size" vs "  Wrong   Size  " would no longer
    dedup and this test would fail."""
    repo = FakeRepository()
    first_llm = FakeLLM(ExtractedRefundFields(order_reference="ORD-1234", reason="wrong size", amount_cents=None))
    second_llm = FakeLLM(
        ExtractedRefundFields(order_reference="ORD-1234", reason="  Wrong   Size  ", amount_cents=None)
    )

    first = submit_chat_message("Refund order #ORD-1234, wrong size", llm=first_llm, repo=repo, now=FIXED_NOW)
    second = submit_chat_message(
        "Refund order #ORD-1234, WRONG SIZE again please", llm=second_llm, repo=repo, now=FIXED_NOW
    )

    assert isinstance(first, IntakeResult) and first.created is True
    assert isinstance(second, IntakeResult) and second.created is False
    assert second.refund_request.id == first.refund_request.id
    assert len(repo.by_dedup_key) == 1  # no duplicate row was created


def test_duplicate_submission_dedups_across_order_reference_casing() -> None:
    repo = FakeRepository()
    first_llm = FakeLLM(ExtractedRefundFields(order_reference="ORD-1234", reason="wrong size", amount_cents=None))
    second_llm = FakeLLM(ExtractedRefundFields(order_reference="ord-1234", reason="wrong size", amount_cents=None))

    first = submit_chat_message("Refund order #ORD-1234, wrong size", llm=first_llm, repo=repo, now=FIXED_NOW)
    second = submit_chat_message("refund order ord-1234, wrong size", llm=second_llm, repo=repo, now=FIXED_NOW)

    assert first.created is True
    assert second.created is False
    assert second.refund_request.id == first.refund_request.id
    # The stored/displayed order_reference is never rewritten by the key
    # normalization -- it keeps whatever the *winning* (first) row had.
    assert second.refund_request.order_reference == "ORD-1234"


def test_missing_order_reference_asks_for_clarification() -> None:
    llm = FakeLLM(ExtractedRefundFields(order_reference="", reason="wants a refund", amount_cents=None))
    repo = FakeRepository()

    result = submit_chat_message("I want a refund", llm=llm, repo=repo, now=FIXED_NOW)

    assert isinstance(result, ClarificationNeeded)
    assert result.message  # non-empty clarification prompt returned, not a crash
    assert len(repo.by_dedup_key) == 0  # no RefundRequest row created


def test_dedup_key_collision_on_save_returns_persisted_row_not_local_one() -> None:
    """If save() reports it lost the insert race (a concurrent submission
    won), submit_chat_message must return the row that actually got
    persisted -- never the locally-constructed RefundRequest that was
    silently discarded."""
    llm = FakeLLM(ExtractedRefundFields(order_reference="ORD-7777", reason="damaged", amount_cents=None))
    concurrent_winner = RefundRequest.new(order_reference="ORD-7777", reason="damaged", amount_cents=500)
    repo = RaceyFakeRepository(concurrent_winner)

    result = submit_chat_message("Refund order #ORD-7777, damaged", llm=llm, repo=repo, now=FIXED_NOW)

    assert isinstance(result, IntakeResult)
    assert result.created is False
    assert result.refund_request.id == concurrent_winner.id
    assert result.refund_request.amount_cents == 500


def test_compute_dedup_key_rejects_naive_datetime() -> None:
    naive_now = datetime(2026, 8, 27, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError):
        compute_dedup_key("ORD-1234", "wrong size", naive_now)


def test_dedup_key_does_not_collide_across_pipe_delimiter_boundaries() -> None:
    """Naive '|'-joined concatenation would make these two different
    (order_reference, reason) pairs serialize to the same string and
    therefore hash to the same key. They must not collide."""
    key_a = compute_dedup_key("ORD-1|extra", "foo", FIXED_NOW)
    key_b = compute_dedup_key("ORD-1", "extra|foo", FIXED_NOW)
    assert key_a != key_b


def test_llm_extraction_failure_raises_extraction_error() -> None:
    llm = RaisingFakeLLM()
    repo = FakeRepository()

    with pytest.raises(ExtractionError):
        submit_chat_message("Refund order #ORD-1234, wrong size", llm=llm, repo=repo, now=FIXED_NOW)

    assert len(repo.by_dedup_key) == 0  # nothing was persisted on failure


def _build_test_app(
    rate_limit: int = 100,
    llm: Optional[LLMPort] = None,
    repo: Optional[RefundRepositoryPort] = None,
) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(chat_routes.router)
    llm = llm or FakeLLM(ExtractedRefundFields(order_reference="ORD-9999", reason="damaged", amount_cents=None))
    repo = repo or FakeRepository()
    limiter = chat_routes.InMemoryRateLimiter(limit=rate_limit, window_seconds=60)

    app.dependency_overrides[chat_routes.get_llm_port] = lambda: llm
    app.dependency_overrides[chat_routes.get_repository_port] = lambda: repo
    app.dependency_overrides[chat_routes.get_rate_limiter] = lambda: limiter
    return app


def test_rate_limit_exceeded_returns_429_with_error_envelope() -> None:
    app = _build_test_app(rate_limit=1)
    client = TestClient(app)

    first = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-9999, damaged"})
    second = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-9999, damaged"})

    assert first.status_code == 200
    assert second.status_code == 429
    body = second.json()
    assert body["error"]["code"] == "rate_limited"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_extraction_failure_returns_502_with_error_envelope() -> None:
    app = _build_test_app(llm=RaisingFakeLLM())
    client = TestClient(app)

    response = client.post("/api/chat/refund-requests", json={"message": "Refund order #ORD-1234, wrong size"})

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "extraction_failed"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
