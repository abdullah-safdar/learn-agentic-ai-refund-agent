"""FastAPI endpoint for chat-based refund submission.

Inbound adapter: translates HTTP <-> domain calls only. Own request/response
DTOs distinct from the domain `RefundRequest` entity (AD-12) -- no endpoint
returns the domain entity directly. Rate limiting is enforced here, at the
API layer (AD-11).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque
from typing import Dict, Deque, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from domain.intake import ClarificationNeeded, submit_chat_message
from domain.ports import LLMPort, RefundRepositoryPort

router = APIRouter()


# --------------------------------------------------------------------------
# Rate limiting (AD-11): enforced at this inbound API adapter, never in the
# domain or in an outbound adapter.
# --------------------------------------------------------------------------


class InMemoryRateLimiter:
    """Fixed-window limiter, keyed per client.

    In-process only -- correct for a single backend instance (this
    project's MVP deployment target), but not shared across instances. A
    multi-instance deployment would need a shared store (e.g. Redis)
    instead; swapping that in doesn't change callers, since they only see
    `.allow(key)`.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        hits = self._hits[key]
        cutoff = current - self._window_seconds
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(current)
        return True


# Ask First (spec, AD-11): no specific limit value was already decided.
# Picking a sensible MVP default here; override via env vars if needed.
CHAT_RATE_LIMIT = int(os.environ.get("CHAT_RATE_LIMIT_PER_WINDOW", "10"))
CHAT_RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("CHAT_RATE_LIMIT_WINDOW_SECONDS", "60"))

_default_rate_limiter = InMemoryRateLimiter(CHAT_RATE_LIMIT, CHAT_RATE_LIMIT_WINDOW_SECONDS)


def get_rate_limiter() -> InMemoryRateLimiter:
    return _default_rate_limiter


# --------------------------------------------------------------------------
# Ports wiring -- overridden at the composition root (api/main.py) via
# `app.dependency_overrides`. Tests override these with fakes directly.
# --------------------------------------------------------------------------


def get_llm_port() -> LLMPort:
    raise NotImplementedError("LLMPort dependency not configured -- see api/main.py")


def get_repository_port() -> RefundRepositoryPort:
    raise NotImplementedError("RefundRepositoryPort dependency not configured -- see api/main.py")


# --------------------------------------------------------------------------
# DTOs (AD-12): distinct types from the domain RefundRequest entity.
# --------------------------------------------------------------------------


class ChatSubmissionRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class RefundRequestSummary(BaseModel):
    """Explicit field allowlist for what a chat client is allowed to see --
    never the domain entity serialized directly."""

    id: str
    order_reference: str
    reason: str
    amount_cents: Optional[int]
    status: str


class ChatSubmissionResponse(BaseModel):
    type: Literal["confirmation", "clarification"]
    message: str
    refund_request: Optional[RefundRequestSummary] = None
    deduplicated: Optional[bool] = None


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


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
def submit_refund_chat_message(
    payload: ChatSubmissionRequest,
    request: Request,
    llm: LLMPort = Depends(get_llm_port),
    repository: RefundRepositoryPort = Depends(get_repository_port),
    rate_limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
) -> ChatSubmissionResponse:
    if not rate_limiter.allow(_client_key(request)):
        raise _rate_limit_error()

    result = submit_chat_message(payload.message, llm=llm, repo=repository)

    if isinstance(result, ClarificationNeeded):
        return ChatSubmissionResponse(type="clarification", message=result.message)

    refund_request = result.refund_request
    summary = RefundRequestSummary(
        id=refund_request.id,
        order_reference=refund_request.order_reference,
        reason=refund_request.reason,
        amount_cents=refund_request.amount_cents,
        status=refund_request.status,
    )
    if result.created:
        confirmation_message = (
            f"Got it -- I've recorded a refund request for order "
            f"{refund_request.order_reference} ({refund_request.reason})."
        )
    else:
        confirmation_message = (
            f"You already have a matching refund request open for order "
            f"{refund_request.order_reference}; no new request was created."
        )

    return ChatSubmissionResponse(
        type="confirmation",
        message=confirmation_message,
        refund_request=summary,
        deduplicated=not result.created,
    )
