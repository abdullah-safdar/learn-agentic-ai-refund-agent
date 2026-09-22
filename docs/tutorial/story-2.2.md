# Stage 2.2: Policy-Cited Compliance Checking Replaces the Hardcoded Rule Set

**Code:** [`backend/services/policy.py`](../../backend/services/policy.py) · [`backend/services/llm.py`](../../backend/services/llm.py) · [`backend/db.py`](../../backend/db.py) · [`backend/services/embeddings.py`](../../backend/services/embeddings.py) · [`backend/services/agent_loop.py`](../../backend/services/agent_loop.py)

## What this stage builds

[Stage 1.2](./story-1.2.md) shipped the Agent Loop's policy check with four hardcoded `if` statements and an empty `citation_ids` list — a placeholder designed to be swapped for real policy reasoning without altering any calling code. [Stage 2.1](./story-2.1.md) ingested the store's actual refund policy documents into a pgvector Policy Store, giving each clause a stable `citation_id`.

This stage completes that swap. It replaces `services/policy.py`'s placeholder rules with a **Retrieval-Augmented Generation (RAG)** pipeline:

1. **Pre-RAG Guards:** Fast, deterministic checks on objective data (order status, requested amount).
2. **Semantic Retrieval:** Embeds a query describing the refund request using Voyage AI and retrieves the top-5 most relevant policy clauses from Postgres using pgvector cosine distance.
3. **Grounded LLM Judgment:** An LLM evaluates the request against *only* the retrieved candidate clauses and returns a structured `PolicyDecision{compliant, confidence, citation_ids}`.
4. **Citation Trust Boundary:** The backend verifies that every cited clause was actually in the retrieved candidate set. If an LLM claims `compliant=True` but failed to cite any valid clauses, the decision is forced to `compliant=False, confidence=0.0`.

Because `evaluate_policy()` still returns the exact same `PolicyDecision` model established in Stage 1.1, the rest of the system — the Agent Loop, the Trajectory event logger, and the Escalation Threshold — needs zero changes beyond passing `refund_request.reason` into the check.

## The shape of the code

```
Customer Request (Order, Amount, Reason)
                  │
                  ▼
       [ Pre-RAG Guards ] ──(fail)──► Non-compliant (confidence=1.0, no I/O)
                  │ (pass)
                  ▼
       [ Embed Query Text ] ────────► Voyage AI (input_type="query")
                  │
                  ▼
       [ Vector Similarity ] ───────► Postgres pgvector (top-5 candidate chunks)
                  │
                  ▼
       [ Structured LLM Eval ] ─────► LLM Provider (Groq / Anthropic / OpenAI)
                  │
                  ▼
       [ Citation Trust Boundary ] ─► Filter hallucinated citations;
                                      force non-compliant if zero valid citations remain
                  │
                  ▼
          PolicyDecision { compliant, confidence, citation_ids }
```

### 1. Pre-RAG Guards — fast, deterministic short-circuits

Before calling any external API, [`evaluate_policy()`](../../backend/services/policy.py#L68) checks objective invariants:
- Does the order exist, and is its status `COMPLETED`?
- Is the requested refund amount a positive integer?
- Is the requested amount less than or equal to the order's total amount?

If any guard fails, it immediately returns `PolicyDecision(compliant=False, confidence=1.0, citation_ids=[])`. No embeddings are calculated, no database queries are run, and zero LLM tokens are consumed.

### 2. Reason-Aware Query Embedding

When guards pass, the agent crafts a query string combining the order status, order date, requested amount, current timestamp, and crucially, the customer's stated `reason`:

```python
# services/policy.py
query_text = (
    f"Order status: {order.status}. "
    f"Order date: {order.order_date}. "
    f"Requested refund amount (cents): {requested_amount_cents}. "
    f"Reason for request: {reason}. "
    f"Current date: {now.isoformat()}."
)
query_embedding = embeddings.embed_texts([query_text], input_type="query")[0]
```

[`embeddings.embed_texts()`](../../backend/services/embeddings.py#L52) now accepts an `input_type` parameter. While document ingestion uses `input_type="document"`, query embedding uses `input_type="query"`. Voyage AI's models use this distinction to apply retrieval-optimized prefix embeddings.

### 3. Cosine Distance Retrieval in Postgres

[`db.search_policy_chunks()`](../../backend/db.py#L654) executes a vector similarity query across all active policy chunks:

```sql
SELECT document_slug, citation_id, chunk_index, content
FROM policy_chunks
WHERE is_active = true
ORDER BY embedding <=> %s::vector
LIMIT %s
```

Using pgvector's cosine distance operator (`<=>`), Postgres orders all currently active chunks and returns the top 5 candidates. If no active chunks exist in the store (e.g. fresh environment before ingestion), the function cleanly returns `PolicyDecision(compliant=False, confidence=0.0, citation_ids=[])` without invoking the LLM.

### 4. Structured LLM Compliance Judgment

[`llm.judge_policy_compliance()`](../../backend/services/llm.py#L303) prompts the LLM with the request context and the retrieved candidate chunks. The model is constrained to output structured JSON matching the `PolicyDecision` schema:

- `compliant` (boolean): whether the request satisfies the store's policy.
- `confidence` (float between 0.0 and 1.0): the model's confidence in its evaluation.
- `citation_ids` (list of strings): the exact `citation_id` values from the candidate chunks supporting the verdict.

Both OpenAI-compatible providers (Groq) via JSON mode and Anthropic via native schema parsing are supported, maintaining the provider portability introduced in Stage 1.1.

### 5. The Citation Trust Boundary

LLMs can hallucinate citations that sound plausible but do not exist in the source document. [`_filter_to_candidate_citation_ids()`](../../backend/services/policy.py#L59) strictly validates the LLM's returned citation list against the set of chunks actually provided in that prompt:

```python
# services/policy.py
candidate_ids = {chunk.citation_id for chunk in candidate_chunks}
filtered_citation_ids = [cid for cid in judgment.citation_ids if cid in candidate_ids]

if judgment.compliant and not filtered_citation_ids:
    return _non_compliant(confidence=_ZERO_CONFIDENCE)
```

If the LLM judged a request as `compliant=True` but cited exclusively hallucinated or out-of-context IDs, the code refuses to trust the verdict. The decision is forced to non-compliant with `confidence=0.0`, protecting downstream money-moving actions from ungrounded approvals.

## The non-obvious decisions

### 1. Guards before RAG: Don't burn tokens on objective facts
Checking whether an order is `COMPLETED` or whether `$150` was requested on a `$100` order is not a semantic policy nuance. It is an objective business fact. Running an LLM on an impossible refund request wastes latency, incurs cost, and introduces non-deterministic failure modes into questions math can answer with 100% certainty.

### 2. Reason-aware retrieval: Why query text needs the customer's words
Early drafts constructed retrieval queries using only order date and amounts. But real refund policies branch on *why* the refund was requested:
- Damaged / defective merchandise often has an extended return window and waived restocking fees.
- "Changed my mind" or buyer's remorse often incurs a restocking fee or requires unopened packaging.
- Final sale items or digital downloads may be non-refundable entirely.

Including `reason` in both the embedded query and the LLM prompt allows semantic vector search to surface the specific exception clauses relevant to the customer's situation.

### 3. Enforcing grounding at code level, not prompt level
Prompting an LLM to "only cite clauses from the provided text" is a suggestion; code enforcement is a guarantee. By treating the retrieved chunks as a closed candidate set and filtering the returned IDs in Python, hallucinated citations can never enter the Trajectory log or convince the Agent Loop to issue an unauthorized refund.

## Try it yourself

Run the updated test suite:

```bash
cd backend
.venv/bin/python -m pytest tests/test_agent_loop.py tests/test_intake.py
.venv/bin/python -m pytest tests/                                          # 104 passed
```

You can also run the full agent loop with an ingested policy:

```bash
cd backend
python -m scripts.ingest_policy_docs
```

## Next stage

[Stage 2.3](../planning/EPICS.md) tackles **conversation context management**: as dialogues grow over multiple turns, older messages are summarized instead of silently dropped, preserving context while staying within token limits. See [Epics & Stories](../planning/EPICS.md) for the full sequence.
