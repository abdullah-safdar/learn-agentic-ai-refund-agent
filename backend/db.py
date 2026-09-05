"""Postgres access: creating the tables, and every read/write the refund
agent needs. Plain functions -- no repository classes, no interfaces. Each
function opens its own short-lived connection; fine for this project's
traffic, swap in a connection pool later if it ever needs to scale.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from models import Order, RefundRequest, TrajectoryEvent

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

# AD-5: append-only, one row per major Agent Loop step, foreign-keyed to its
# RefundRequest. sequence_no is domain-assigned (db.record_trajectory_event)
# and transactionally unique per refund_request_id -- the UNIQUE constraint
# is the backstop, not the primary mechanism (see record_trajectory_event's
# docstring). No update/delete path exists anywhere in this module.
CREATE_TRAJECTORY_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trajectory_events (
    id UUID PRIMARY KEY,
    refund_request_id UUID NOT NULL REFERENCES refund_requests(id),
    sequence_no INTEGER NOT NULL,
    step_type TEXT NOT NULL,
    step_data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (refund_request_id, sequence_no)
);
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


def _row_to_trajectory_event(row: dict[str, Any]) -> TrajectoryEvent:
    return TrajectoryEvent(
        id=str(row["id"]),
        refund_request_id=str(row["refund_request_id"]),
        sequence_no=row["sequence_no"],
        step_type=row["step_type"],
        step_data=row["step_data"],
        created_at=_to_iso8601_utc(row["created_at"]),
    )


def run_migrations() -> None:
    """Create the refund_requests, orders, and trajectory_events tables if
    they don't already exist. Idempotent -- safe to call on every
    startup."""
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_REFUND_REQUESTS_TABLE_SQL)
            cur.execute(CREATE_ORDERS_TABLE_SQL)
            cur.execute(CREATE_TRAJECTORY_EVENTS_TABLE_SQL)
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


def find_refund_request_by_id(refund_request_id: str) -> Optional[RefundRequest]:
    """The RefundRequest with this id, or None if no such row exists --
    including when `refund_request_id` isn't even a well-formed UUID.
    Validated here in Python *before* ever touching Postgres, so the
    trajectory endpoint's 404 path doesn't need to special-case malformed
    path parameters -- and so a genuine data-layer error against a
    well-formed UUID surfaces as a real error instead of being silently
    swallowed into a 404 by a broad `except psycopg.errors.DataError`."""
    try:
        uuid.UUID(refund_request_id)
    except ValueError:
        return None
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, order_reference, reason, amount_cents, status, created_at
                FROM refund_requests
                WHERE id = %s
                """,
                (refund_request_id,),
            )
            row = cur.fetchone()
    return _row_to_refund_request(row) if row is not None else None


def record_trajectory_event(
    refund_request_id: str, step_type: str, step_data: dict[str, Any], now: datetime
) -> TrajectoryEvent:
    """Insert one immutable TrajectoryEvent row (AD-5). `step_data` must
    already be the redacted, allowlisted dict for `step_type` -- built by
    the caller (agent_loop.py); this function only persists it, never
    shapes it.

    `sequence_no` is assigned transactionally, in the same transaction as
    the insert: `SELECT ... FOR UPDATE` locks this refund_request_id's
    existing trajectory_events rows first, then `MAX(sequence_no) + 1` (or
    `1` if none exist yet) is computed in Python. `UNIQUE(refund_request_id,
    sequence_no)` is the backstop if this ever races -- Epic 1's loop runs
    synchronously with a single writer per request, so this is
    correctness-focused, not a throughput concern.
    """
    event_id = uuid.uuid4()
    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sequence_no
                FROM trajectory_events
                WHERE refund_request_id = %s
                FOR UPDATE
                """,
                (refund_request_id,),
            )
            existing_sequence_nos = [row[0] for row in cur.fetchall()]
            next_sequence_no = max(existing_sequence_nos, default=0) + 1
            cur.execute(
                """
                INSERT INTO trajectory_events
                    (id, refund_request_id, sequence_no, step_type, step_data, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (event_id, refund_request_id, next_sequence_no, step_type, Jsonb(step_data), now),
            )
        conn.commit()
    return TrajectoryEvent(
        id=str(event_id),
        refund_request_id=refund_request_id,
        sequence_no=next_sequence_no,
        step_type=step_type,
        step_data=step_data,
        created_at=_to_iso8601_utc(now),
    )


def list_trajectory_events(refund_request_id: str) -> List[TrajectoryEvent]:
    """Every TrajectoryEvent row for this refund_request_id, ordered by
    sequence_no ascending -- the order the steps actually happened in. An
    empty list means the RefundRequest exists but hasn't recorded any steps
    yet (never used to mean "not found"; callers check that via
    `find_refund_request_by_id` first)."""
    with psycopg.connect(_dsn(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, refund_request_id, sequence_no, step_type, step_data, created_at
                FROM trajectory_events
                WHERE refund_request_id = %s
                ORDER BY sequence_no ASC
                """,
                (refund_request_id,),
            )
            rows = cur.fetchall()
    return [_row_to_trajectory_event(row) for row in rows]


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
