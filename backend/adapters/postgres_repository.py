"""RefundRepositoryPort implementation; migration for `RefundRequest` only
(DB-per-story principle -- Story 1.2+ add Order/Trajectory/etc. tables
separately).

Pure translation layer: SQL in, domain `RefundRequest` out. No business
logic (dedup key computation, reason normalization, ...) lives here -- that
all stays in `domain/intake.py` per AD-3 ("adapters never perform their own
independent dedupe").
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from domain.models import RefundRequest
from domain.ports import RefundRepositoryPort

CREATE_TABLE_SQL = """
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


class PostgresRefundRepository(RefundRepositoryPort):
    """RefundRepositoryPort backed by PostgreSQL, via psycopg3.

    Opens a short-lived connection per call rather than holding one open --
    simple and correct for this story's traffic; swap in a connection pool
    (`psycopg_pool`) if/when volume warrants it without changing the port.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def run_migrations(self) -> None:
        """Create the `refund_requests` table if it doesn't already exist.
        Intentionally creates only this one table -- no Order, Trajectory,
        or ApprovalQueueEntry tables belong in this story.
        """
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
            conn.commit()

    def find_by_dedup_key(self, dedup_key: str) -> Optional[RefundRequest]:
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
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

    def save(self, refund_request: RefundRequest, dedup_key: str) -> bool:
        with psycopg.connect(self._dsn) as conn:
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
                # ON CONFLICT DO NOTHING makes rowcount 0 when a row already
                # existed under this dedup_key (lost the race) and 1 when
                # this call actually inserted -- the domain relies on this
                # to know whether to trust the row it just built locally.
                inserted = cur.rowcount == 1
            conn.commit()
        return inserted
