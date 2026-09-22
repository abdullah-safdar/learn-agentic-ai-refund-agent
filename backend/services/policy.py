"""Policy/compliance check: does this refund request pass the store's
rules?

Guards (below) are deterministic, no-I/O checks on objective data -- order
existence/status, requested-amount sanity. Once they pass, this is no
longer a pure function (spec-2-2): it embeds a query describing the
request, retrieves the most relevant chunks of the store's real, ingested
refund policy documents (the Policy Store -- spec-2-1) by pgvector cosine
distance, and asks an LLM to judge compliance grounded only in those
retrieved chunks, citing which clause(s) it relied on. The return-window
rule that used to be a hardcoded constant here now lives in the Policy
Store's actual documents instead, and is judged the same RAG-backed way as
every other clause.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

import db
from models import ORDER_STATUS_COMPLETED, Order, PolicyChunk, PolicyDecision
from services import embeddings, llm

# Guard failures are deterministic yes/no checks, not a probabilistic
# judgment -- there's no partial-confidence case to express, so a guard
# failure always reports full confidence in the "non-compliant" it decided.
_FULL_CONFIDENCE = 1.0

# Ask First: zero candidate chunks retrieved (e.g. an empty Policy Store)
# means there is nothing to check compliance against -- reported as a hard
# non-compliant with zero confidence (distinct from a guard failure's full-
# confidence non-compliant), and no LLM call is made.
_ZERO_CONFIDENCE = 0.0

# Ask First: top-K chunks retrieved per decision.
TOP_K_CANDIDATE_CHUNKS = 5


def _non_compliant(confidence: float = _FULL_CONFIDENCE) -> PolicyDecision:
    return PolicyDecision(compliant=False, confidence=confidence, citation_ids=[])


def _build_query_text(order: Order, requested_amount_cents: int, reason: str, now: datetime) -> str:
    """Ask First: the query text embedded for retrieval -- order status,
    order date, requested amount, the customer's stated reason, and the
    current date, as plain text. `reason` lets retrieval find
    reason-specific clauses (e.g. defective item vs. changed-of-mind) that
    an order/amount-only query couldn't distinguish (spec-2-2 change log)."""
    return (
        f"Order status: {order.status}. "
        f"Order date: {order.order_date}. "
        f"Requested refund amount (cents): {requested_amount_cents}. "
        f"Reason for request: {reason}. "
        f"Current date: {now.isoformat()}."
    )


def _filter_to_candidate_citation_ids(citation_ids: List[str], candidate_chunks: List[PolicyChunk]) -> List[str]:
    """Citation trust boundary (Design Notes): the LLM may only echo back
    citation_ids from the closed candidate set it was given. Anything else
    is a bug or hallucination and is dropped here, before a PolicyDecision
    is ever constructed -- never trusted through to the Trajectory."""
    candidate_ids = {chunk.citation_id for chunk in candidate_chunks}
    return [citation_id for citation_id in citation_ids if citation_id in candidate_ids]


def evaluate_policy(
    order: Optional[Order], requested_amount_cents: Optional[int], reason: str, now: datetime
) -> PolicyDecision:
    """Decide whether a refund request complies with the store's policy.

    Pre-RAG guards short-circuit on objective data problems: the order must
    exist and be COMPLETED, and the requested amount must be a positive
    integer that doesn't exceed the order's own amount. Any guard failure
    returns non-compliant immediately, with no embedding/LLM call and
    citation_ids=[] (nothing was consulted).

    Once guards pass: embed a query built from the order + requested amount
    + reason (input_type="query"), retrieve the top-K most relevant active
    Policy Store chunks across every ingested document
    (db.search_policy_chunks, pgvector cosine distance), and ask an LLM
    (services/llm.judge_policy_compliance) to judge compliance grounded
    only in those retrieved chunks. The LLM's citation_ids are filtered
    down to that exact candidate set before this returns; if that leaves a
    compliant judgment with zero real citations, it is forced to
    non-compliant/confidence 0.0 instead (an ungrounded "compliant" is
    never returned).

    An empty retrieval result (nothing ingested yet, or nothing relevant)
    is treated as non-compliant with confidence 0.0 and no LLM call.

    Embedding/LLM/DB failures are never caught here -- they propagate as
    raised exceptions for agent_loop.run()'s existing outer try/except to
    turn into Failed(reason=...). Silently defaulting to compliant or
    non-compliant on a collaborator failure would be worse than surfacing
    it.
    """
    if order is None or order.status != ORDER_STATUS_COMPLETED:
        return _non_compliant()

    if requested_amount_cents is None or requested_amount_cents <= 0:
        return _non_compliant()

    if requested_amount_cents > order.amount_cents:
        return _non_compliant()

    query_text = _build_query_text(order, requested_amount_cents, reason, now)
    query_embedding = embeddings.embed_texts([query_text], input_type="query")[0]
    candidate_chunks = db.search_policy_chunks(query_embedding, TOP_K_CANDIDATE_CHUNKS)

    if not candidate_chunks:
        return _non_compliant(confidence=_ZERO_CONFIDENCE)

    judgment = llm.judge_policy_compliance(order, requested_amount_cents, reason, now, candidate_chunks)
    filtered_citation_ids = _filter_to_candidate_citation_ids(judgment.citation_ids, candidate_chunks)

    if judgment.compliant and not filtered_citation_ids:
        # spec-2-2 change log: every citation the LLM gave turned out to be
        # hallucinated/invalid -- a compliant decision must never leave this
        # function with zero real citations backing it, so this is treated
        # the same as a zero-chunks-retrieved result rather than trusting
        # the LLM's own compliant flag.
        return _non_compliant(confidence=_ZERO_CONFIDENCE)

    return PolicyDecision(
        compliant=judgment.compliant,
        confidence=judgment.confidence,
        citation_ids=filtered_citation_ids,
    )
