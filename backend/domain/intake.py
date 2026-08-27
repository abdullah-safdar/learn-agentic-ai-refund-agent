"""Intake-dedup + RefundRequest creation flow (AD-3).

Order of operations matters (see the spec's Design Notes): run extraction
first (to get `order_reference`), *then* compute the dedup key and check for
an existing row, *then* create a new row only if none matched. Checking
dedup before extraction isn't possible -- there's nothing to key on yet.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Union

from domain.models import RefundRequest
from domain.ports import LLMPort, RefundRepositoryPort

# Ask First (spec): the exact intake-dedup time window wasn't already
# decided, so this picks the spec's own suggested default of 5 minutes.
# Flag for renegotiation if a different value is wanted.
DEDUP_WINDOW_SECONDS = 300

_WHITESPACE_RE = re.compile(r"\s+")


class ExtractionError(Exception):
    """Raised when the LLMPort fails to produce extraction output at all
    (e.g. the provider call errors out). Distinct from "nothing was
    stated in the message", which is a normal ClarificationNeeded result,
    not an error.
    """


@dataclass(frozen=True)
class ClarificationNeeded:
    """No RefundRequest was created; the customer should be asked to
    clarify (I/O matrix: "Missing order reference")."""

    message: str


@dataclass(frozen=True)
class IntakeResult:
    """A RefundRequest now exists for this submission -- either freshly
    created, or an existing row reused via intake dedup."""

    refund_request: RefundRequest
    created: bool


IntakeOutcome = Union[IntakeResult, ClarificationNeeded]


def normalize_reason(reason: str) -> str:
    """Deterministic normalization for the dedup key: lowercase, trimmed,
    internal whitespace collapsed to single spaces."""
    return _WHITESPACE_RE.sub(" ", reason.strip().lower())


def _normalize_order_reference_for_key(order_reference: str) -> str:
    """Normalization applied only when computing the dedup key -- casing
    differences (e.g. "ORD-1234" vs "ord-1234") shouldn't defeat dedup. The
    stored/displayed `order_reference` value itself is never altered."""
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
    time_window) per AD-3. `time_window` is folded in as a bucketed window
    index (`now` divided into DEDUP_WINDOW_SECONDS-wide buckets) so the
    repository can do a plain equality lookup instead of a time-range query.

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


def submit_chat_message(
    chat_text: str,
    llm: LLMPort,
    repo: RefundRepositoryPort,
    now: Optional[datetime] = None,
) -> IntakeOutcome:
    """Extract a fixed schema from `chat_text`, dedup-check, and create a
    RefundRequest if needed. This is the single entry point adapters
    (the chat API route) should call -- it owns the ordering AD-3 requires.
    """
    current_time = now or datetime.now(timezone.utc)

    try:
        extracted = llm.extract_refund_request(chat_text)
    except Exception as exc:  # noqa: BLE001 -- translate any adapter failure
        raise ExtractionError(f"LLMPort extraction failed: {exc}") from exc

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

    existing = repo.find_by_dedup_key(dedup_key)
    if existing is not None:
        return IntakeResult(refund_request=existing, created=False)

    refund_request = RefundRequest.new(
        order_reference=order_reference,
        reason=reason,
        amount_cents=extracted.amount_cents,
    )
    inserted = repo.save(refund_request, dedup_key)
    if not inserted:
        # Lost a concurrent insert race: some other submission persisted a
        # row under this exact dedup_key between our find_by_dedup_key
        # check above and this save() call. The locally-built
        # `refund_request` above was never actually persisted -- returning
        # it would silently misreport a phantom row's id/fields to the
        # caller. Re-fetch and hand back the row that actually won instead.
        winning_request = repo.find_by_dedup_key(dedup_key)
        if winning_request is None:
            raise RuntimeError(
                "RefundRepositoryPort.save() reported no insert occurred, "
                "but no row was found for its dedup_key on re-fetch."
            )
        return IntakeResult(refund_request=winning_request, created=False)

    return IntakeResult(refund_request=refund_request, created=True)
