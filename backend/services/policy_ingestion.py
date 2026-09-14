"""Chunks a refund policy document by clause and persists it into the
Policy Store (spec-2-1). `chunk_policy_document()` is pure -- no I/O, same
convention as services/policy.py's module docstring. `ingest_document()` is
the only function here that talks to services/embeddings.py and db.py, and
it does so in two strictly separate steps: embed everything first, persist
second -- so an embedding-API failure never leaves a document
half-superseded in the Policy Store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import List

import db
from models import PolicyChunk
from services import embeddings

# Clause boundaries are `## N. Title` markdown headers -- exactly two `#`
# characters followed by a space, so a deeper `### Sub-heading` (which also
# starts with "##") never matches and stays part of its enclosing clause's
# content instead of starting a new chunk.
_CLAUSE_HEADER_RE = re.compile(r"^## (.+?)\s*$", re.MULTILINE)

# Collapses any run of non-alphanumeric characters to a single hyphen, then
# trims leading/trailing hyphens -- e.g. "1. Return Window" -> "1-return-window".
_SLUG_COLLAPSE_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ClauseChunk:
    """One clause, chunked but not yet embedded or persisted --
    chunk_policy_document()'s output, ingest_document()'s input."""

    citation_id: str
    chunk_index: int
    content: str


def slugify(text: str) -> str:
    """Public (no leading underscore) because scripts/ingest_policy_docs.py
    also uses this to derive `document_slug` from a filename stem -- both
    the document-id half and the clause-id half of a citation_id go through
    the exact same slug convention."""
    return _SLUG_COLLAPSE_RE.sub("-", text.strip().lower()).strip("-")


def chunk_policy_document(document_slug: str, document_text: str) -> List[ClauseChunk]:
    """Split `document_text` into one ClauseChunk per `## N. Title` header
    -- never a fixed-size token window, so a citation_id survives
    re-ingestion of an unrelated, unchanged clause elsewhere in the same
    document.

    `citation_id` is `{document_slug}#{clause_slug}`, where `clause_slug` is
    derived from the heading text itself (including its number) -- stable
    across a content edit within the clause, but changed by renaming or
    reordering the heading, since the slug comes from the heading text, not
    from content or position.

    Any document text before the first clause header (a top-level `# Title`
    line, an intro paragraph, ...) is intentionally excluded from every
    chunk -- it is never embedded and never retrievable by citation_id, not
    a bug. Only text from a `## N. Title` header onward is chunked.

    Raises ValueError if no clause headers are found at all -- the
    "Malformed document" edge case: callers must never proceed to embed or
    persist zero chunks for a document. Also raises ValueError if a heading
    slugifies to an empty string (e.g. a bare `## ` with no title text), or
    if two headings in this same document slugify to the same clause_slug
    -- either would otherwise let two chunks silently share one ambiguous
    citation_id, both `is_active=true` at once.
    """
    matches = list(_CLAUSE_HEADER_RE.finditer(document_text))
    if not matches:
        raise ValueError(
            f"No '## N. Title' clause headers found in document {document_slug!r} -- "
            "refusing to ingest a document with zero clauses."
        )

    chunks: List[ClauseChunk] = []
    seen_citation_ids: set[str] = set()
    for index, match in enumerate(matches):
        heading_text = match.group(1).strip()
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(document_text)
        content = document_text[start:end].strip()
        clause_slug = slugify(heading_text)
        if not clause_slug:
            raise ValueError(
                f"Clause heading {index} in document {document_slug!r} ({heading_text!r}) "
                "slugifies to an empty string -- refusing to produce an ambiguous citation_id."
            )
        citation_id = f"{document_slug}#{clause_slug}"
        if citation_id in seen_citation_ids:
            raise ValueError(
                f"Duplicate citation_id {citation_id!r} in document {document_slug!r} -- two "
                "clause headings slugify to the same value. Rename one of the headings."
            )
        seen_citation_ids.add(citation_id)
        chunks.append(ClauseChunk(citation_id=citation_id, chunk_index=index, content=content))
    return chunks


def ingest_document(document_slug: str, document_text: str, now: datetime) -> List[PolicyChunk]:
    """Chunk, embed, and persist one policy document, superseding any
    previously-active chunks for `document_slug` in a single DB transaction
    (db.replace_policy_document_chunks).

    Order of operations matters here: chunk_policy_document() (pure, and
    the source of the "malformed document" raise) runs first, then every
    chunk's embedding is fetched via embeddings.embed_texts() *before*
    db.replace_policy_document_chunks() ever opens its transaction. An
    embedding-API failure therefore raises straight out of this function
    with zero DB writes -- any previously-active chunks for this document
    are left exactly as they were.
    """
    clause_chunks = chunk_policy_document(document_slug, document_text)
    vectors = embeddings.embed_texts([chunk.content for chunk in clause_chunks])
    rows = [
        (chunk.citation_id, chunk.chunk_index, chunk.content, vector)
        for chunk, vector in zip(clause_chunks, vectors)
    ]
    return db.replace_policy_document_chunks(document_slug, rows, now)
