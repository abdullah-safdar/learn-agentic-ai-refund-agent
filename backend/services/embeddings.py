"""Embeds text for the Policy Store ingestion pipeline (spec-2-1) and for
policy retrieval queries at decision time (spec-2-2, services/policy.py).
Always calls Voyage AI (`voyage-4-lite`), independent of the `LLM_PROVIDER`
env var that llm.py switches on -- a deliberate asymmetry (Ask First):
Anthropic does not offer its own embedding model (its docs point to Voyage
AI as the recommended provider), and none of this project's other
configured chat providers (groq/openai/xai) are being used for embeddings
here either, so there is nothing to switch between.

Duplicates llm.py's lazy-client-cache shape (`_get_client()`) locally rather
than importing llm.py's private function -- this module has exactly one
provider, so there's no per-provider table to share, and reaching into
another module's private helper would couple two things that should stay
independent.
"""

from __future__ import annotations

import os
from typing import List, Optional

import voyageai

DEFAULT_MODEL = "voyage-4-lite"

# voyage-4-lite's default output size -- also policy_chunks.embedding's
# fixed column type (db.py: `VECTOR(1024)`). EMBEDDING_MODEL is freely
# overridable (see below), but the table's column width is not, so a
# different-dimension model must fail loudly here rather than at INSERT
# time with a cryptic Postgres error.
EXPECTED_EMBEDDING_DIM = 1024

# Lazily-built, cached client so importing this module never requires
# VOYAGE_API_KEY to be set -- only actually calling embed_texts() does
# (same convention as llm.py's _openai_compatible_clients cache).
_client: Optional[voyageai.Client] = None


def _get_client() -> voyageai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("VOYAGE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "VOYAGE_API_KEY must be set to run policy ingestion -- embeddings always call "
                "Voyage AI, independent of LLM_PROVIDER."
            )
        _client = voyageai.Client(api_key=api_key)
    return _client


def embed_texts(texts: List[str], input_type: str = "document") -> List[List[float]]:
    """Embed a batch of texts via Voyage AI, returning one embedding vector
    per input text, in the same order `texts` was given. Raises on any API
    failure -- callers (services/policy_ingestion.py's ingest_document(),
    services/policy.py's evaluate_policy()) must let this propagate rather
    than catching it, so an embedding failure never leaves a document's
    Policy Store rows half-superseded, and never silently defaults a policy
    decision.

    `input_type` defaults to `"document"` -- ingestion (policy_ingestion.py)
    always indexes policy clauses for later retrieval and relies on that
    default. Retrieval-time callers (policy.py, embedding a decision's
    query text) must instead pass `input_type="query"`. Voyage's own
    guidance is to always set this parameter explicitly (never leave it
    `None`) for retrieval/RAG use cases, since it changes the prompt Voyage
    prepends internally before embedding, and document/query texts should
    get different prompts.

    Also raises if the response doesn't contain exactly one embedding per
    input text, or if any returned embedding's dimension doesn't match
    EXPECTED_EMBEDDING_DIM -- policy_chunks.embedding is a fixed-width
    `VECTOR(1024)` column, so an EMBEDDING_MODEL override to a
    different-dimension model must fail here with a clear error rather than
    at INSERT time with a cryptic Postgres one.
    """
    if not texts:
        return []
    client = _get_client()
    model = os.environ.get("EMBEDDING_MODEL", DEFAULT_MODEL)
    response = client.embed(texts, model=model, input_type=input_type)
    if len(response.embeddings) != len(texts):
        raise RuntimeError(
            f"Voyage AI embeddings response returned {len(response.embeddings)} embedding(s) for "
            f"{len(texts)} input text(s) -- expected exactly one per input."
        )
    for embedding in response.embeddings:
        if len(embedding) != EXPECTED_EMBEDDING_DIM:
            raise RuntimeError(
                f"Embedding model {model!r} returned a {len(embedding)}-dimension vector, "
                f"but policy_chunks.embedding is a fixed VECTOR({EXPECTED_EMBEDDING_DIM}) column. "
                "Set EMBEDDING_MODEL to a model that produces "
                f"{EXPECTED_EMBEDDING_DIM}-dimension embeddings, or widen the column."
            )
    return response.embeddings
