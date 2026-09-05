"""Shared data shapes for the refund agent: what a RefundRequest/Order looks
like, and the small "what happened" result types each step can return.

These are plain dataclasses -- Python's version of a TypeScript `type` or
`interface`. `frozen=True` makes an instance immutable after creation (like
`Object.freeze()`, but enforced by the language). `Union[...]` is
TypeScript's `|`: a value that can be one of several shapes, told apart at
runtime with `isinstance(value, SomeShape)` (Python's `instanceof`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_ESCALATED = "escalated"
STATUS_FAILED = "failed"

ORDER_STATUS_COMPLETED = "completed"

# TrajectoryEvent.step_type values -- one row per major Agent Loop step
# (AD-5), never per retry. Order matches the sequence a NewRequestInput run
# actually happened in when every step is reached.
STEP_TYPE_ORDER_LOOKUP = "order_lookup"
STEP_TYPE_POLICY_DECISION = "policy_decision"
STEP_TYPE_STRIPE_REFUND = "stripe_refund"
STEP_TYPE_OUTCOME = "outcome"


def utc_now_iso8601() -> str:
    """Current UTC time as an ISO-8601 string with a trailing Z, millisecond
    precision (compact, sortable, unambiguous)."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class RefundRequest:
    id: str
    order_reference: str
    reason: str
    amount_cents: Optional[int]
    status: str
    created_at: str

    @staticmethod
    def new(order_reference: str, reason: str, amount_cents: Optional[int]) -> "RefundRequest":
        """The only place `id`/`created_at` should be generated from --
        callers should never construct these fields themselves."""
        return RefundRequest(
            id=str(uuid.uuid4()),
            order_reference=order_reference,
            reason=reason,
            amount_cents=amount_cents,
            status=STATUS_PENDING,
            created_at=utc_now_iso8601(),
        )


@dataclass(frozen=True)
class Order:
    """The original e-commerce transaction a RefundRequest is checked
    against. Resolved by `db.find_order_by_reference()`; never
    created/mutated by this system except via the dev-admin tooling in
    `dev_admin.py`."""

    id: str
    order_reference: str
    status: str
    amount_cents: int
    order_date: str
    stripe_payment_intent_id: Optional[str]


@dataclass(frozen=True)
class ExtractedRefundFields:
    """What `llm.extract_refund_request()` returns. `order_reference`/
    `reason` are always strings -- an empty string means the field wasn't
    stated in the customer's message. `amount_cents` may be null if
    unstated."""

    order_reference: str
    reason: str
    amount_cents: Optional[int]


@dataclass(frozen=True)
class TrajectoryEvent:
    """One immutable row in a RefundRequest's reasoning trajectory (AD-5) --
    inserted by `db.record_trajectory_event()`, never updated or deleted.
    `sequence_no` is domain-assigned, monotonic per `refund_request_id`
    (never inferred from `created_at`). `step_data` holds only that
    `step_type`'s allowlisted, redacted fields -- built by
    `agent_loop._record_step()` -- never a raw Tool payload, exception, or
    customer-supplied text."""

    id: str
    refund_request_id: str
    sequence_no: int
    step_type: str
    step_data: Dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class PolicyDecision:
    """What `policy.evaluate_policy()` returns -- a fixed shape regardless
    of how the decision was actually made (today: hardcoded rules; a later
    stage: an LLM reasoning over real policy documents). `citation_ids`
    stays empty until that exists."""

    compliant: bool
    confidence: float
    citation_ids: List[str]


# --- Intake outcomes (intake.py's submit_chat_message) ---------------------


@dataclass(frozen=True)
class ClarificationNeeded:
    """No RefundRequest was created; the customer should be asked to
    clarify (e.g. no order number found in their message)."""

    message: str


@dataclass(frozen=True)
class IntakeResult:
    """A RefundRequest now exists for this submission -- either freshly
    created, or an existing row reused via intake dedup."""

    refund_request: RefundRequest
    created: bool


IntakeOutcome = Union[IntakeResult, ClarificationNeeded]


# --- Agent Loop input/output (agent_loop.py's run()) ------------------------


@dataclass(frozen=True)
class NewRequestInput:
    """Forward execution: resolve a brand-new RefundRequest."""

    refund_request: RefundRequest


@dataclass(frozen=True)
class ResumeInput:
    """Resume after a human reviewer acts on an Escalated request -- not
    handled yet, but the shape already exists so `run()`'s signature won't
    need to change when it is."""

    refund_request_id: str
    reviewer_decision: str


RunInput = Union[NewRequestInput, ResumeInput]


@dataclass(frozen=True)
class Completed:
    refund_id: str
    amount_cents: int


@dataclass(frozen=True)
class Escalated:
    pending_review_id: str
    tentative_recommendation: str


@dataclass(frozen=True)
class Failed:
    reason: str


AgentResult = Union[Completed, Escalated, Failed]
