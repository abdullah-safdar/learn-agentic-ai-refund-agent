"""Unit tests for the I/O & Edge-Case Matrix in
spec-2-1-ingest-the-store-s-refund-policy-documents.md.

`chunk_policy_document()` is exercised directly (pure, no I/O).
`ingest_document()` is exercised with `services.embeddings.embed_texts` and
`db.replace_policy_document_chunks` monkeypatched -- the same
FakeRepo/install_repo() monkeypatch-helper pattern as
tests/test_intake.py's FakeRepo. No real Voyage AI or Postgres calls.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Sequence, Tuple

import pytest

import db
from models import PolicyChunk
from services import embeddings, policy_ingestion

FIXED_NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)

WELL_FORMED_DOC = """# Sample Policy

## 1. Return Window
Refunds must be requested within 30 days.

## 2. Non-Returnable Categories
Gift cards are non-returnable.
"""


# --------------------------------------------------------------------------
# Fakes + monkeypatch helpers
# --------------------------------------------------------------------------


class FakePolicyChunkRepo:
    """In-memory stand-in for db.py's policy_chunks persistence -- keeps
    every row ever inserted (active and superseded), exactly like the real
    supersede-not-delete table (old rows are never hard-deleted)."""

    def __init__(self) -> None:
        self.rows: List[PolicyChunk] = []

    def replace(
        self,
        document_id: str,
        chunks: Sequence[Tuple[str, int, str, List[float]]],
        now: datetime,
    ) -> List[PolicyChunk]:
        self.rows = [self._deactivate_if_matching(row, document_id) for row in self.rows]
        new_rows = [
            PolicyChunk(
                id=str(uuid.uuid4()),
                document_id=document_id,
                citation_id=citation_id,
                chunk_index=chunk_index,
                content=content,
                is_active=True,
                created_at=now.isoformat(),
            )
            for citation_id, chunk_index, content, _embedding in chunks
        ]
        self.rows.extend(new_rows)
        return new_rows

    @staticmethod
    def _deactivate_if_matching(row: PolicyChunk, document_id: str) -> PolicyChunk:
        if row.document_id != document_id or not row.is_active:
            return row
        return PolicyChunk(
            id=row.id,
            document_id=row.document_id,
            citation_id=row.citation_id,
            chunk_index=row.chunk_index,
            content=row.content,
            is_active=False,
            created_at=row.created_at,
        )

    def active_for(self, document_id: str) -> List[PolicyChunk]:
        return [row for row in self.rows if row.document_id == document_id and row.is_active]


def install_repo(monkeypatch: pytest.MonkeyPatch, repo: FakePolicyChunkRepo) -> None:
    monkeypatch.setattr(db, "replace_policy_document_chunks", repo.replace)


def install_embeddings(monkeypatch: pytest.MonkeyPatch, dim: int = 3) -> None:
    def _fake_embed(texts: List[str]) -> List[List[float]]:
        return [[float(len(text))] * dim for text in texts]

    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed)


def install_raising_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(texts: List[str]) -> List[List[float]]:
        raise RuntimeError("simulated embedding API failure")

    monkeypatch.setattr(embeddings, "embed_texts", _raise)


# --------------------------------------------------------------------------
# chunk_policy_document() -- pure chunker
# --------------------------------------------------------------------------


def test_chunk_policy_document_splits_by_clause_header() -> None:
    chunks = policy_ingestion.chunk_policy_document("sample-policy", WELL_FORMED_DOC)

    assert [c.citation_id for c in chunks] == [
        "sample-policy#1-return-window",
        "sample-policy#2-non-returnable-categories",
    ]
    assert [c.chunk_index for c in chunks] == [0, 1]
    assert "30 days" in chunks[0].content
    assert "Gift cards" in chunks[1].content


def test_chunk_policy_document_raises_when_no_clause_headers_found() -> None:
    with pytest.raises(ValueError):
        policy_ingestion.chunk_policy_document("bad-doc", "# Title\n\nNo clause headers here.\n")


def test_chunk_policy_document_raises_on_duplicate_citation_id() -> None:
    """Two headings that slugify to the same clause_slug would otherwise
    both land as is_active=true under one ambiguous citation_id."""
    doc = """## 1. Return Window
First version.

## 1. Return Window
Accidentally duplicated heading.
"""
    with pytest.raises(ValueError):
        policy_ingestion.chunk_policy_document("dup-doc", doc)


def test_chunk_policy_document_raises_on_heading_that_slugifies_to_empty_string() -> None:
    """A heading made up entirely of punctuation collapses to an empty
    clause_slug -- '## !!!' has non-empty heading text (matches the header
    regex) but nothing alphanumeric for slugify() to keep."""
    doc = """## !!!
Body with a punctuation-only heading.
"""
    with pytest.raises(ValueError):
        policy_ingestion.chunk_policy_document("punctuation-heading-doc", doc)


def test_chunk_policy_document_ignores_deeper_subheadings() -> None:
    """A '### Sub-heading' also starts with '##' -- must not be mistaken
    for a clause boundary."""
    doc = """## 1. Title
Body.

### Not a clause boundary
Still part of clause 1.

## 2. Second
Second body.
"""
    chunks = policy_ingestion.chunk_policy_document("doc", doc)
    assert len(chunks) == 2
    assert "Not a clause boundary" in chunks[0].content


# --------------------------------------------------------------------------
# ingest_document() -- embeds then persists
# --------------------------------------------------------------------------


def test_first_ingest_creates_one_active_row_per_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakePolicyChunkRepo()
    install_repo(monkeypatch, repo)
    install_embeddings(monkeypatch)

    result = policy_ingestion.ingest_document("sample-policy", WELL_FORMED_DOC, FIXED_NOW)

    assert len(result) == 2
    assert all(row.is_active for row in result)
    citation_ids = {row.citation_id for row in result}
    assert citation_ids == {
        "sample-policy#1-return-window",
        "sample-policy#2-non-returnable-categories",
    }


def test_reingest_with_changed_content_supersedes_old_rows_keeps_unrelated_citation_ids_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakePolicyChunkRepo()
    install_repo(monkeypatch, repo)
    install_embeddings(monkeypatch)

    policy_ingestion.ingest_document("sample-policy", WELL_FORMED_DOC, FIXED_NOW)
    updated_doc = WELL_FORMED_DOC.replace("30 days", "45 days")
    policy_ingestion.ingest_document("sample-policy", updated_doc, FIXED_NOW)

    active_rows = repo.active_for("sample-policy")
    assert len(active_rows) == 2
    assert len(repo.rows) == 4  # 2 superseded + 2 active
    assert sum(1 for row in repo.rows if not row.is_active) == 2

    active_citation_ids = {row.citation_id for row in active_rows}
    # unrelated clause's citation_id is unchanged across re-ingestion
    assert "sample-policy#2-non-returnable-categories" in active_citation_ids
    active_return_window = next(r for r in active_rows if r.citation_id == "sample-policy#1-return-window")
    assert "45 days" in active_return_window.content


def test_reingest_with_clause_removed_leaves_no_active_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakePolicyChunkRepo()
    install_repo(monkeypatch, repo)
    install_embeddings(monkeypatch)

    policy_ingestion.ingest_document("sample-policy", WELL_FORMED_DOC, FIXED_NOW)
    doc_missing_clause_2 = """# Sample Policy

## 1. Return Window
Refunds must be requested within 30 days.
"""
    policy_ingestion.ingest_document("sample-policy", doc_missing_clause_2, FIXED_NOW)

    active_rows = repo.active_for("sample-policy")
    assert len(active_rows) == 1
    assert active_rows[0].citation_id == "sample-policy#1-return-window"
    superseded_citation_ids = {row.citation_id for row in repo.rows if not row.is_active}
    assert "sample-policy#2-non-returnable-categories" in superseded_citation_ids


def test_embedding_api_failure_leaves_no_db_writes_and_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakePolicyChunkRepo()
    install_repo(monkeypatch, repo)
    install_raising_embeddings(monkeypatch)

    with pytest.raises(RuntimeError):
        policy_ingestion.ingest_document("sample-policy", WELL_FORMED_DOC, FIXED_NOW)

    assert repo.rows == []


def test_embedding_api_failure_leaves_previously_active_chunks_active(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = FakePolicyChunkRepo()
    install_repo(monkeypatch, repo)
    install_embeddings(monkeypatch)
    policy_ingestion.ingest_document("sample-policy", WELL_FORMED_DOC, FIXED_NOW)
    previously_active = {row.citation_id for row in repo.active_for("sample-policy")}

    install_raising_embeddings(monkeypatch)
    with pytest.raises(RuntimeError):
        policy_ingestion.ingest_document(
            "sample-policy", WELL_FORMED_DOC.replace("30 days", "60 days"), FIXED_NOW
        )

    still_active = {row.citation_id for row in repo.active_for("sample-policy")}
    assert still_active == previously_active


def test_malformed_document_raises_before_any_embedding_or_db_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"embed": 0, "db": 0}

    def _embed(texts: List[str]) -> List[List[float]]:
        calls["embed"] += 1
        return [[0.0] for _ in texts]

    def _replace(document_id: str, chunks, now: datetime) -> List[PolicyChunk]:
        calls["db"] += 1
        return []

    monkeypatch.setattr(embeddings, "embed_texts", _embed)
    monkeypatch.setattr(db, "replace_policy_document_chunks", _replace)

    with pytest.raises(ValueError):
        policy_ingestion.ingest_document("bad-doc", "# Title\nNo clause headers.\n", FIXED_NOW)

    assert calls == {"embed": 0, "db": 0}
