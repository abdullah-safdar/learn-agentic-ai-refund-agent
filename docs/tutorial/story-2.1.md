# Stage 2.1: Ingest the Store's Refund Policy Documents

**Code:** [`backend/services/policy_ingestion.py`](../../backend/services/policy_ingestion.py) · [`backend/services/embeddings.py`](../../backend/services/embeddings.py) · [`backend/scripts/ingest_policy_docs.py`](../../backend/scripts/ingest_policy_docs.py) · [`backend/db.py`](../../backend/db.py) · [`backend/models.py`](../../backend/models.py)

## What this stage builds

[Stage 1.2](./story-1.2.md) shipped the Agent Loop's policy check as a deliberate placeholder — four hardcoded `if` statements — with a note that it was designed to be swapped for a real AI-driven check later. This stage is the first half of that swap. It doesn't touch the decision logic at all yet; it builds the **Policy Store**: a pipeline that takes the store's actual refund policy documents (plain markdown files) and turns them into searchable, embedded rows in Postgres, each one retrievable by a stable `citation_id`.

Nothing reads from the Policy Store yet. [Stage 2.2](../planning/EPICS.md) is the one that replaces `services/policy.py`'s hardcoded rules with a retrieval-backed adapter that actually queries these rows and cites them in its decision. This stage only writes.

## The shape of the code

Three pieces, each doing exactly one job:

### 1. Chunking — pure, no I/O

[`chunk_policy_document()`](../../backend/services/policy_ingestion.py#L50) splits a document's text on `## N. Title` markdown headers — never a fixed-size token window. Each clause becomes one chunk, and its `citation_id` is derived from the heading text itself: `{document_slug}#{clause_slug}` (e.g. `general-refund-policy#1-return-window`). That means a `citation_id` survives re-ingestion of an unrelated clause elsewhere in the same document — only renaming, reordering, or removing *that* heading changes *its* id. Text before the first clause header (a top-level title, an intro paragraph) is intentionally never chunked or embedded.

It raises `ValueError` on anything that would produce an ambiguous store: zero clause headers found, a heading that slugifies to an empty string, or two headings colliding on the same slug.

### 2. Embedding — always Voyage AI, regardless of `LLM_PROVIDER`

[`embed_texts()`](../../backend/services/embeddings.py#L54) sends a batch of chunk texts to Voyage AI's `voyage-4-lite` and returns one 1024-dimension vector per input, in input order. This is a deliberate asymmetry: the rest of the backend can run on Anthropic, Groq, or xAI via `LLM_PROVIDER`, but embeddings only ever come from Voyage AI, because Anthropic doesn't offer its own embedding model (its own docs point to Voyage AI as the recommended provider) and none of this project's other configured chat providers serve embeddings either. `VOYAGE_API_KEY` is required to *run* ingestion but not to run the API itself — and Voyage's free tier (200M tokens, no credit card) comfortably covers this project's scale.

Every input is embedded with `input_type="document"` — these texts are always policy clauses being indexed for later retrieval, never a search query, and Voyage's own guidance is to always set this parameter for retrieval/RAG use cases (it changes a prompt Voyage prepends internally before embedding). A defensive check also compares every returned embedding's dimension against `policy_chunks.embedding`'s fixed `VECTOR(1024)` column width, so pointing `EMBEDDING_MODEL` at a different-dimension model fails here, loudly, instead of at `INSERT` time with a cryptic Postgres error.

### 3. Persisting — supersede, never overwrite

[`ingest_document()`](../../backend/services/policy_ingestion.py#L106) wires the two together in a strict order: chunk everything, embed everything, *then* call [`db.replace_policy_document_chunks()`](../../backend/db.py#L573). That ordering is the point — see below.

`replace_policy_document_chunks()` runs inside one transaction: every currently-active `policy_chunks` row for that `document_id` is marked `is_active = false`, then every freshly-embedded chunk is inserted as `is_active = true`. Superseded rows are never hard-deleted — a `citation_id` some past policy decision already cited must keep resolving even after the document that produced it is edited and re-ingested.

The whole thing is driven by a small CLI, [`scripts/ingest_policy_docs.py`](../../backend/scripts/ingest_policy_docs.py): it walks every `.md` file under `backend/policy_documents/`, ingests each independently, and reports which documents (if any) failed — one bad document never blocks the others, since each is its own transaction.

```python
# services/policy_ingestion.py — ingest_document()
clause_chunks = chunk_policy_document(document_slug, document_text)
vectors = embeddings.embed_texts([chunk.content for chunk in clause_chunks])
rows = [
    (chunk.citation_id, chunk.chunk_index, chunk.content, vector)
    for chunk, vector in zip(clause_chunks, vectors)
]
return db.replace_policy_document_chunks(document_slug, rows, now)
```

## The non-obvious decision: embed *before* opening the transaction

`ingest_document()` could have inserted each chunk right after embedding it, one at a time, inside the same transaction as the supersede `UPDATE`. It deliberately doesn't. Every embedding for the whole document is fetched first, fully, and held in memory — and only once every single one has succeeded does `replace_policy_document_chunks()` ever touch the database.

The reason is that an embedding call and a database write are two systems with no shared transaction between them. If chunk 3 of 4 failed to embed *after* chunk 1 and 2 were already written and the old chunks already marked inactive, the document would be left half-superseded: some old clauses gone, some new clauses missing, no clean state to retry from or roll back to. By finishing every external API call first, an embedding failure raises straight out of `ingest_document()` with the database untouched — the previous ingestion's rows are exactly as they were, and re-running the script is always safe.

It's the same underlying concern as [Stage 1.2's Stripe step](./story-1.2.md) (an external call that can't be safely half-done) and [Stage 1.6's concurrent-decision guard](./story-1.6.md) (never leave one system updated and its counterpart stale) — just resolved here by re-ordering *when* the risky call happens, rather than by a retry policy or a database-level guard.

## Try it yourself

```bash
cd backend
pytest tests/test_embeddings.py tests/test_policy_ingestion.py tests/test_db_policy_chunks.py   # 20 tests - chunking, embedding, and the supersede transaction
pytest tests/                                                                                    # 99 tests -- full suite, Stages 1.1-1.6 + this stage
```

Running the real pipeline needs `DATABASE_URL` and `VOYAGE_API_KEY` set (see `.env.example`):

```bash
cd backend
python -m scripts.ingest_policy_docs
# Ingested general-refund-policy.md: 4 active chunk(s) under document_slug='general-refund-policy'
# Ingested restocking-and-exceptions.md: 3 active chunk(s) under document_slug='restocking-and-exceptions'
```

Re-run it any time, including with an edited document — it's idempotent by design: the previous run's chunks for that document are superseded, never duplicated.

## Next stage

Stage 2.2 (not yet built) reads what this stage writes: it replaces `services/policy.py`'s hardcoded rule set with a retrieval-backed adapter that queries the Policy Store and returns a real `PolicyDecision{compliant, confidence, citation_ids}` — the same port shape the Agent Loop already calls, so no caller code changes. See [Epics & Stories](../planning/EPICS.md) for the full planned sequence.
