"""Domain entities.

No imports from `adapters/` or `api/` are allowed in this module or anywhere
else under `domain/` (AD-1). Domain nouns match the PRD Glossary verbatim.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# `RefundRequest.status` is the sole canonical lifecycle field (AD-7). This
# story (1.1) only ever creates requests in the "pending" state -- later
# stories (policy check, auto-approval, escalation, ...) add further status
# values and the transitions between them.
STATUS_PENDING = "pending"


def utc_now_iso8601() -> str:
    """Return the current UTC time as an ISO-8601 string with a trailing Z.

    Millisecond precision keeps timestamps compact while remaining sortable
    and unambiguous.
    """
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class RefundRequest:
    """The domain entity every later story extends.

    Fields intentionally match the Code Map: id, order_reference, reason,
    amount_cents, status, created_at. `order_reference` is a plain string in
    this story -- validating it against a real Order is Story 1.2, not here.
    """

    id: str
    order_reference: str
    reason: str
    amount_cents: Optional[int]
    status: str
    created_at: str

    @staticmethod
    def new(order_reference: str, reason: str, amount_cents: Optional[int]) -> "RefundRequest":
        """Create a brand-new RefundRequest with a fresh UUIDv4 id and the
        current UTC timestamp. This is the only place `id`/`created_at`
        should be generated from -- callers should never construct these
        fields themselves.
        """
        return RefundRequest(
            id=str(uuid.uuid4()),
            order_reference=order_reference,
            reason=reason,
            amount_cents=amount_cents,
            status=STATUS_PENDING,
            created_at=utc_now_iso8601(),
        )
