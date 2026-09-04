"""Dev-only tooling: seed test Orders (via a real Stripe test-mode
PaymentIntent) and list Orders/RefundRequests directly, bypassing the chat
intake and Agent Loop entirely. Never called from chat_api.py or
agent_loop.py -- purely for manually testing the chat flow without
hand-written SQL or curl calls against Stripe.

Kept separate from db.py's find_order_by_reference/etc. deliberately: those
exist for the Agent Loop's actual needs (single order lookup, single-row
save); bolting "list everything"/"create arbitrary rows" onto them would
blur what the real production functions are for.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
import stripe
from psycopg.rows import dict_row

DEFAULT_TIMEOUT_SECONDS = 12.0

# A Stripe test-mode card that always succeeds when confirmed immediately --
# fine for seeding test Orders; Stripe itself rejects it outside test mode.
_TEST_PAYMENT_METHOD = "pm_card_visa"

_client: Optional[stripe.StripeClient] = None


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL must be set to run the API.")
    return dsn


def _get_client() -> stripe.StripeClient:
    global _client
    if _client is None:
        api_key = os.environ.get("STRIPE_SECRET_KEY")
        if not api_key:
            raise RuntimeError("STRIPE_SECRET_KEY must be set to run the API.")
        if not api_key.startswith("sk_test_"):
            raise RuntimeError(
                "Dev admin tooling requires a Stripe test-mode secret key (starting with 'sk_test_')."
            )
        _client = stripe.StripeClient(
            api_key,
            http_client=stripe.RequestsClient(timeout=DEFAULT_TIMEOUT_SECONDS),
            max_network_retries=0,
        )
    return _client


def _to_iso8601_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def list_orders() -> list[dict[str, Any]]:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, order_reference, status, amount_cents, order_date, stripe_payment_intent_id
                FROM orders
                ORDER BY order_date DESC
                """
            )
            rows = cur.fetchall()
    return [
        {
            "id": str(row["id"]),
            "order_reference": row["order_reference"],
            "status": row["status"],
            "amount_cents": row["amount_cents"],
            "order_date": _to_iso8601_utc(row["order_date"]),
            "stripe_payment_intent_id": row["stripe_payment_intent_id"],
        }
        for row in rows
    ]


def list_refund_requests() -> list[dict[str, Any]]:
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, order_reference, reason, amount_cents, status, created_at
                FROM refund_requests
                ORDER BY created_at DESC
                """
            )
            rows = cur.fetchall()
    return [
        {
            "id": str(row["id"]),
            "order_reference": row["order_reference"],
            "reason": row["reason"],
            "amount_cents": row["amount_cents"],
            "status": row["status"],
            "created_at": _to_iso8601_utc(row["created_at"]),
        }
        for row in rows
    ]


def create_order(order_reference: str, amount_cents: int) -> dict[str, Any]:
    """Creates a real Stripe test-mode PaymentIntent first, and only
    inserts the Order row if that succeeds -- an Order with no genuine
    PaymentIntent behind it would fail confusingly the moment someone tries
    to refund it (stripe_refund.py)."""
    client = _get_client()
    payment_intent = client.v1.payment_intents.create(
        params={
            "amount": amount_cents,
            "currency": "usd",
            "payment_method": _TEST_PAYMENT_METHOD,
            "confirm": True,
            "automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
        }
    )

    order_id = uuid.uuid4()
    order_date = datetime.now(timezone.utc)
    # Relies on the caller to translate psycopg.errors.UniqueViolation
    # (duplicate order_reference, case-insensitive) into a clean HTTP
    # response -- same functional unique index as db.py.
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO orders (id, order_reference, status, amount_cents, order_date, stripe_payment_intent_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (order_id, order_reference, "completed", amount_cents, order_date, payment_intent.id),
            )
        conn.commit()

    return {
        "id": str(order_id),
        "order_reference": order_reference,
        "status": "completed",
        "amount_cents": amount_cents,
        "order_date": _to_iso8601_utc(order_date),
        "stripe_payment_intent_id": payment_intent.id,
    }
