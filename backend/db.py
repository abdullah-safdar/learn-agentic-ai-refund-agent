"""Postgres access: creating the tables, and every read/write the refund
agent needs. Plain functions -- no repository classes, no interfaces. Each
function opens its own short-lived connection; fine for this project's
traffic, swap in a connection pool later if it ever needs to scale.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from models import Order, RefundRequest

CREATE_REFUND_REQUESTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS refund_requests (
    id UUID PRIMARY KEY,
    order_reference TEXT NOT NULL,
    reason TEXT NOT NULL,
    amount_cents BIGINT,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE
);
"""

# The unique index is *functional*, on lower(order_reference) -- matches
# find_order_by_reference()'s case-insensitive lookup exactly. A plain
# UNIQUE on order_reference would let "ORD-1" and "ord-1" both insert, then
# collide unpredictably at lookup time depending on which one is matched
# first.
CREATE_ORDERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY,
    order_reference TEXT NOT NULL,
    status TEXT NOT NULL,
    amount_cents BIGINT NOT NULL,
    order_date TIMESTAMPTZ NOT NULL,
    stripe_payment_intent_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_order_reference_lower_idx
    ON orders (lower(order_reference));
"""

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0


def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL must be set to run the API.")
    return dsn


def _to_iso8601_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _row_to_refund_request(row: dict[str, Any]) -> RefundRequest:
    return RefundRequest(
        id=str(row["id"]),
        order_reference=row["order_reference"],
        reason=row["reason"],
        amount_cents=row["amount_cents"],
        status=row["status"],
        created_at=_to_iso8601_utc(row["created_at"]),
    )


def _row_to_order(row: dict[str, Any]) -> Order:
    return Order(
        id=str(row["id"]),
        order_reference=row["order_reference"],
        status=row["status"],
        amount_cents=row["amount_cents"],
        order_date=_to_iso8601_utc(row["order_date"]),
        stripe_payment_intent_id=row["stripe_payment_intent_id"],
    )


def run_migrations() -> None:
    """Create the refund_requests and orders tables if they don't already
    exist. Idempotent -- safe to call on every startup."""
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_REFUND_REQUESTS_TABLE_SQL)
            cur.execute(CREATE_ORDERS_TABLE_SQL)
        conn.commit()


def find_refund_request_by_dedup_key(dedup_key: str) -> Optional[RefundRequest]:
    """The RefundRequest previously saved under this exact intake-dedup
    key, or None if no such row exists."""
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, order_reference, reason, amount_cents, status, created_at
                FROM refund_requests
                WHERE dedup_key = %s
                """,
                (dedup_key,),
            )
            row = cur.fetchone()
    return _row_to_refund_request(row) if row is not None else None


def save_refund_request(refund_request: RefundRequest, dedup_key: str) -> bool:
    """Insert a newly created RefundRequest under `dedup_key`. A collision
    on `dedup_key` is a no-op, not an error -- a concurrent duplicate
    submission can race this same check-then-create.

    Returns True if this call actually inserted a new row, False if a row
    already existed under `dedup_key` (some other request won the race) --
    callers use this to know whether to trust the row they just built
    locally, or re-fetch the one that actually got persisted.
    """
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO refund_requests
                    (id, order_reference, reason, amount_cents, status, created_at, dedup_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (dedup_key) DO NOTHING
                """,
                (
                    refund_request.id,
                    refund_request.order_reference,
                    refund_request.reason,
                    refund_request.amount_cents,
                    refund_request.status,
                    refund_request.created_at,
                    dedup_key,
                ),
            )
            inserted = cur.rowcount == 1
        conn.commit()
    return inserted


def update_refund_status(refund_request_id: str, status: str) -> None:
    """Persist a resolved outcome onto the RefundRequest row."""
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE refund_requests SET status = %s WHERE id = %s",
                (status, refund_request_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"update_refund_status() matched {cur.rowcount} rows for "
                    f"refund_request_id={refund_request_id!r} -- expected exactly 1."
                )
        conn.commit()


def find_order_by_reference(order_reference: str) -> Optional[Order]:
    """Case-insensitive lookup by order_reference. Returns None if no
    matching order exists -- a legitimate "not found" outcome. Raises
    (never returns a sentinel) on a genuine I/O failure so
    `agent_loop.run()` can tell "not found" apart from "lookup failed" when
    deciding whether to retry.
    """
    with psycopg.connect(
        _dsn(),
        row_factory=dict_row,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={int(DEFAULT_CONNECT_TIMEOUT_SECONDS * 1000)}",
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, order_reference, status, amount_cents, order_date, stripe_payment_intent_id
                FROM orders
                WHERE lower(order_reference) = lower(%s)
                """,
                (order_reference,),
            )
            row = cur.fetchone()
    return _row_to_order(row) if row is not None else None
