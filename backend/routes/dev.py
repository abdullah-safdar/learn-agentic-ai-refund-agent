"""Dev-only endpoints for seeding test Orders and inspecting
Orders/RefundRequests directly -- bypasses the Agent Loop and the chat
intake path entirely.

Exists purely to make manually testing the chat flow easier (no
hand-written SQL, no manual Stripe curl calls). Never used by the real
product surface (chat_api.py) and carries no auth, matching this project's
"no customer identity/auth" scope decision -- kept under an /api/dev prefix
so that split stays obvious to anyone reading the route table.
"""

from __future__ import annotations

from typing import List, Optional

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services import dev_admin

router = APIRouter()


class OrderSummary(BaseModel):
    id: str
    order_reference: str
    status: str
    amount_cents: int
    order_date: str
    stripe_payment_intent_id: Optional[str]


class RefundRequestSummary(BaseModel):
    id: str
    order_reference: str
    reason: str
    amount_cents: Optional[int]
    status: str
    created_at: str


class CreateOrderRequest(BaseModel):
    order_reference: str = Field(min_length=1, max_length=100)
    amount_cents: int = Field(gt=0)


@router.get("/api/dev/orders", response_model=List[OrderSummary])
def list_orders() -> List[OrderSummary]:
    return [OrderSummary(**order) for order in dev_admin.list_orders()]


@router.post("/api/dev/orders", response_model=OrderSummary, status_code=201)
def create_order(payload: CreateOrderRequest) -> OrderSummary:
    try:
        order = dev_admin.create_order(payload.order_reference, payload.amount_cents)
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "duplicate_order_reference",
                    "message": f"An order with reference {payload.order_reference!r} already exists.",
                    "details": None,
                }
            },
        ) from exc
    return OrderSummary(**order)


@router.get("/api/dev/refund-requests", response_model=List[RefundRequestSummary])
def list_refund_requests() -> List[RefundRequestSummary]:
    return [RefundRequestSummary(**refund_request) for refund_request in dev_admin.list_refund_requests()]
