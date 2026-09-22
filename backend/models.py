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
# spec-1-6: STATUS_APPROVED is a short-lived interim status, needed only so
# db.record_reviewer_decision()'s atomic `WHERE status = 'escalated'` guard
# has somewhere to transition *to* before the Agent Loop actually resolves
# the resume -- it's overwritten with the real outcome moments later, in the
# same request, once run() returns (see routes/approvals.py). It should
# never be user-visible for more than the duration of one HTTP request.
# STATUS_DENIED is terminal -- deny never calls Stripe and ends the case in
# one step.
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"

ORDER_STATUS_COMPLETED = "completed"

# TrajectoryEvent.step_type values -- one row per major Agent Loop step
# (AD-5), never per retry. Order matches the sequence a NewRequestInput run
# actually happened in when every step is reached.
STEP_TYPE_ORDER_LOOKUP = "order_lookup"
STEP_TYPE_POLICY_DECISION = "policy_decision"
STEP_TYPE_STRIPE_REFUND = "stripe_refund"
STEP_TYPE_OUTCOME = "outcome"
# spec-1-6: recorded once per ResumeInput, before either branch (deny/
# approve) proceeds -- captures who decided what, distinct from the
# customer-facing steps above.
STEP_TYPE_REVIEWER_DECISION = "reviewer_decision"


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
class EscalationThreshold:
    """One versioned/audited row from the `escalation_thresholds` table
    (AD-9) -- insert-only, never updated in place. `db.get_current_escalation_threshold()`
    resolves whichever row is currently in effect (latest `effective_at <=
    now`); a future admin story can add a write path against this same
    table, but this story only reads it."""

    confidence_threshold: float
    dollar_threshold_cents: int
    effective_at: str
    changed_by: str


@dataclass(frozen=True)
class PolicyDecision:
    """What `policy.evaluate_policy()` returns -- a fixed shape regardless
    of how the decision was actually made. A pre-RAG guard failure or an
    empty/ungrounded retrieval still returns this shape with
    `citation_ids=[]`; a real RAG-backed judgment (spec-2-2) returns at
    least one real `citation_id` whenever `compliant=True`."""

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
    """Resume after a human reviewer acts on an Escalated request
    (spec-1-6). `reviewer_identifier` is who decided -- recorded onto the
    STEP_TYPE_REVIEWER_DECISION trajectory step by agent_loop._resume_request,
    independently of (but consistently with) the same identifier already
    persisted onto ApprovalQueueEntry by db.record_reviewer_decision()."""

    refund_request_id: str
    reviewer_decision: str
    reviewer_identifier: str


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


@dataclass(frozen=True)
class Denied:
    """A reviewer denied the request (spec-1-6) -- a real terminal business
    outcome, distinct from Failed (an unexpected/internal failure) and from
    Completed (money actually moved). Deny never calls Stripe."""

    refund_request_id: str


AgentResult = Union[Completed, Escalated, Failed, Denied]


# --- Policy Store (spec-2-1) -------------------------------------------------


@dataclass(frozen=True)
class PolicyChunk:
    """One clause of an ingested refund policy document, embedded and
    persisted by services/policy_ingestion.py -- inserted by
    db.replace_policy_document_chunks(), never updated in place (supersede,
    not overwrite). `citation_id` (`{document_slug}#{clause_slug}`) is
    stable across re-ingestion of unrelated clauses; only this exact
    clause's own removal/rename/reorder changes it. `document_id` is the
    document_slug the chunk came from -- there is no separate `documents`
    table this story. `is_active=False` rows are never hard-deleted: a past
    decision's citation_ids must keep resolving even after the document
    that produced them changes. Story 2.2's retrieval-backed Policy/Decision
    adapter is this dataclass's first real reader; this story only writes
    it."""

    id: str
    document_id: str
    citation_id: str
    chunk_index: int
    content: str
    is_active: bool
    created_at: str


# --- Staff Approval Queue (spec-1-6) ----------------------------------------


@dataclass(frozen=True)
class ApprovalQueueEntry:
    """One immutable row recording a reviewer's decision on an Escalated
    RefundRequest -- inserted by db.record_reviewer_decision() in the same
    DB transaction as the RefundRequest.status transition it guards.
    Append-only, like TrajectoryEvent, and never read to decide "is this
    approved" -- RefundRequest.status stays the sole canonical lifecycle
    field (AD-7); this table exists purely as an audit trail of who decided
    what and when."""

    id: str
    refund_request_id: str
    decision: str
    reviewer_identifier: str
    decided_at: str
