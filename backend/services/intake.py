"""Turns a chat message into a RefundRequest: extract fields, check for a
duplicate submission, create a new row if none matched.

Order of operations matters: extraction first (to get order_reference),
*then* compute the dedup key and check for an existing row, *then* create a
new row only if none matched -- there's nothing to key on before extraction
runs.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

import db
from models import ClarificationNeeded, IntakeOutcome, IntakeResult, RefundRequest
from services import llm

# The dedup time window wasn't pinned down elsewhere, so this picks a
# sensible default. Change it here if a different value is wanted.
DEDUP_WINDOW_SECONDS = 300

_WHITESPACE_RE = re.compile(r"\s+")


class ExtractionError(Exception):
    """Raised when llm.extract_refund_request() fails outright (e.g. the
    provider call errors out) -- distinct from "nothing was stated in the
    message", which is a normal ClarificationNeeded result, not an error.
    """


def normalize_reason(reason: str) -> str:
    """Deterministic normalization for the dedup key: lowercase, trimmed,
    internal whitespace collapsed to single spaces."""
    return _WHITESPACE_RE.sub(" ", reason.strip().lower())


def _normalize_order_reference_for_key(order_reference: str) -> str:
    """Normalization applied only when computing the dedup key -- casing
    differences (e.g. "ORD-1234" vs "ord-1234") shouldn't defeat dedup. The
    stored/displayed order_reference itself is never altered."""
    return order_reference.strip().lower()


def _encode_key_parts(*parts: str) -> bytes:
    """Length-prefix each part before concatenating.

    Plain `"|".join(parts)` is ambiguous: a literal `|` inside one field can
    make two genuinely different (order_reference, reason) pairs serialize
    to the identical string (e.g. ("a|b", "c") and ("a", "b|c")), which
    would then hash to the same dedup key and incorrectly collide. Encoding
    each part's exact byte length up front makes the boundaries unambiguous
    regardless of what characters the parts contain.
    """
    encoded = bytearray()
    for part in parts:
        part_bytes = part.encode("utf-8")
        encoded += len(part_bytes).to_bytes(8, "big")
        encoded += part_bytes
    return bytes(encoded)


def compute_dedup_key(order_reference: str, reason: str, now: datetime) -> str:
    """Deterministic key from (order_reference, normalized_reason,
    time_window). `time_window` is folded in as a bucketed window index
    (`now` divided into DEDUP_WINDOW_SECONDS-wide buckets) so a lookup can
    be a plain equality check instead of a time-range query.

    `now` must be timezone-aware. A naive datetime would be interpreted as
    local server time by `.timestamp()`, silently shifting dedup window
    boundaries depending on the server's timezone -- callers must be
    explicit about UTC instead.
    """
    if now.tzinfo is None:
        raise ValueError(
            "compute_dedup_key requires a timezone-aware `now` -- a naive "
            "datetime would be bucketed using local server time instead of "
            "UTC, which can silently shift dedup window boundaries."
        )

    normalized_order_reference = _normalize_order_reference_for_key(order_reference)
    normalized_reason = normalize_reason(reason)
    window_bucket = int(now.timestamp() // DEDUP_WINDOW_SECONDS)
    key_material = _encode_key_parts(normalized_order_reference, normalized_reason, str(window_bucket))
    return hashlib.sha256(key_material).hexdigest()


def submit_chat_message(chat_text: str, now: Optional[datetime] = None) -> IntakeOutcome:
    """Extract a fixed schema from `chat_text`, dedup-check, and create a
    RefundRequest if needed. The single entry point the chat API route
    calls -- it owns the ordering above."""
    current_time = now or datetime.now(timezone.utc)

    try:
        extracted = llm.extract_refund_request(chat_text)
    except Exception as exc:  # noqa: BLE001 -- translate any provider failure
        raise ExtractionError(f"LLM extraction failed: {exc}") from exc

    order_reference = extracted.order_reference.strip()
    if not order_reference:
        return ClarificationNeeded(
            message=(
                "I couldn't find an order number in your message. Could you "
                "share the order number for the refund you'd like to request?"
            )
        )

    reason = extracted.reason.strip()
    dedup_key = compute_dedup_key(order_reference, reason, current_time)

    existing = db.find_refund_request_by_dedup_key(dedup_key)
    if existing is not None:
        return IntakeResult(refund_request=existing, created=False)

    refund_request = RefundRequest.new(
        order_reference=order_reference,
        reason=reason,
        amount_cents=extracted.amount_cents,
    )
    inserted = db.save_refund_request(refund_request, dedup_key)
    if not inserted:
        # Lost a concurrent insert race: some other submission persisted a
        # row under this exact dedup_key between our find/save calls above.
        # The locally-built refund_request was never actually persisted --
        # re-fetch and hand back the row that actually won instead.
        winning_request = db.find_refund_request_by_dedup_key(dedup_key)
        if winning_request is None:
            raise RuntimeError(
                "save_refund_request() reported no insert occurred, but no "
                "row was found for its dedup_key on re-fetch."
            )
        return IntakeResult(refund_request=winning_request, created=False)

    return IntakeResult(refund_request=refund_request, created=True)
