"""Unit tests for db.py's Policy Store persistence functions (spec-2-1) --
replace_policy_document_chunks() and list_active_policy_chunks().

A fake psycopg connection/cursor stands in for Postgres here, mirroring
tests/test_agent_loop.py's FakeDecisionCursor/FakeDecisionConnection pattern
for record_reviewer_decision(). Every other db.py function in this codebase
is instead monkeypatched wholesale at its own boundary by its callers'
tests (tests/test_intake.py's install_repo(), tests/test_policy_ingestion.py's
FakePolicyChunkRepo) -- right for exercising *callers* of db.py, but it
would leave db.py's own SQL text, bound-param order, and the
register_vector() call on the connection completely unverified. That's
what these tests check instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import uuid4

import pytest

import db

FIXED_NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakePolicyChunksCursor:
    def __init__(self, select_rows: Optional[List[dict]] = None, events: Optional[List[str]] = None) -> None:
        self._select_rows = select_rows or []
        # (normalized_sql, params) for every execute() call, in order --
        # lets tests assert on the actual bound values, not just which
        # statement ran.
        self.calls: List[Tuple[str, Optional[tuple]]] = []
        self._events = events if events is not None else []

    def execute(self, sql: str, params: Optional[tuple] = None) -> None:
        normalized = " ".join(sql.split())
        self.calls.append((normalized, params))
        self._events.append(f"execute:{normalized.split()[0]}")

    def fetchall(self) -> List[dict]:
        return self._select_rows

    def __enter__(self) -> "FakePolicyChunksCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class FakePolicyChunksConnection:
    def __init__(self, cursor: FakePolicyChunksCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self) -> FakePolicyChunksCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> "FakePolicyChunksConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False  # never suppress -- propagate like a real psycopg connection


def test_replace_policy_document_chunks_issues_guarded_update_then_inserts_after_register_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserts the actual SQL/params replace_policy_document_chunks() sends
    to Postgres -- the guarded UPDATE (is_active=false WHERE document_id=...
    AND is_active=true), one INSERT per chunk with embedding bound in the
    right position, and that register_vector(conn) runs before any INSERT
    -- a removed register_vector() call would otherwise still pass every
    test in tests/test_policy_ingestion.py, since those monkeypatch this
    function away entirely."""
    events: List[str] = []
    cursor = FakePolicyChunksCursor(events=events)
    conn = FakePolicyChunksConnection(cursor)
    monkeypatch.setattr(db, "_dsn", lambda: "fake-dsn")
    monkeypatch.setattr(db.psycopg, "connect", lambda *args, **kwargs: conn)

    def _fake_register_vector(connection: FakePolicyChunksConnection) -> None:
        assert connection is conn
        events.append("register_vector")

    monkeypatch.setattr(db, "register_vector", _fake_register_vector)

    chunks = [
        ("doc-a#1-title", 0, "content one", [0.1, 0.2]),
        ("doc-a#2-title", 1, "content two", [0.3, 0.4]),
    ]

    result = db.replace_policy_document_chunks("doc-a", chunks, FIXED_NOW)

    assert len(result) == 2
    assert all(row.is_active for row in result)
    assert conn.committed is True

    # register_vector() must run before the first INSERT -- an embedding
    # can only be bound into an INSERT's params once the connection's
    # vector adapter is registered.
    first_insert_index = events.index("execute:INSERT")
    assert events.index("register_vector") < first_insert_index

    update_sql, update_params = cursor.calls[0]
    assert update_sql.startswith("UPDATE policy_chunks SET is_active = false")
    assert "WHERE document_id = %s AND is_active = true" in update_sql
    assert update_params == ("doc-a",)

    insert_calls = [call for call in cursor.calls if call[0].startswith("INSERT")]
    assert len(insert_calls) == 2

    first_insert_sql, first_insert_params = insert_calls[0]
    assert first_insert_sql.startswith("INSERT INTO policy_chunks")
    assert "is_active, created_at" in first_insert_sql
    # bound params: (id, document_id, citation_id, chunk_index, content, embedding, created_at)
    assert first_insert_params is not None
    assert first_insert_params[1:] == ("doc-a", "doc-a#1-title", 0, "content one", [0.1, 0.2], FIXED_NOW)

    second_insert_params = insert_calls[1][1]
    assert second_insert_params is not None
    assert second_insert_params[1:] == ("doc-a", "doc-a#2-title", 1, "content two", [0.3, 0.4], FIXED_NOW)

    # the two INSERTed ids are distinct, freshly-generated UUIDs
    assert first_insert_params[0] != second_insert_params[0]


def test_replace_policy_document_chunks_returns_no_rows_when_all_clauses_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty `chunks` sequence (every previously-active clause was
    removed from the source document) issues only the superseding UPDATE
    and no INSERT -- the "clause removed, no active replacement" edge case
    at the db.py level."""
    cursor = FakePolicyChunksCursor()
    conn = FakePolicyChunksConnection(cursor)
    monkeypatch.setattr(db, "_dsn", lambda: "fake-dsn")
    monkeypatch.setattr(db.psycopg, "connect", lambda *args, **kwargs: conn)
    monkeypatch.setattr(db, "register_vector", lambda connection: None)

    result = db.replace_policy_document_chunks("doc-a", [], FIXED_NOW)

    assert result == []
    assert len(cursor.calls) == 1
    assert cursor.calls[0][0].startswith("UPDATE policy_chunks")
    assert conn.committed is True


def test_list_active_policy_chunks_selects_only_active_rows_ordered_by_chunk_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_rows = [
        {
            "id": uuid4(),
            "document_id": "doc-a",
            "citation_id": "doc-a#1-title",
            "chunk_index": 0,
            "content": "content one",
            "is_active": True,
            "created_at": FIXED_NOW,
        },
        {
            "id": uuid4(),
            "document_id": "doc-a",
            "citation_id": "doc-a#2-title",
            "chunk_index": 1,
            "content": "content two",
            "is_active": True,
            "created_at": FIXED_NOW,
        },
    ]
    cursor = FakePolicyChunksCursor(select_rows=select_rows)
    conn = FakePolicyChunksConnection(cursor)
    monkeypatch.setattr(db, "_dsn", lambda: "fake-dsn")
    monkeypatch.setattr(db.psycopg, "connect", lambda *args, **kwargs: conn)

    result = db.list_active_policy_chunks("doc-a")

    assert [row.citation_id for row in result] == ["doc-a#1-title", "doc-a#2-title"]
    assert all(row.is_active for row in result)
    assert all(row.document_id == "doc-a" for row in result)

    select_sql, select_params = cursor.calls[0]
    assert select_sql.startswith("SELECT")
    assert "FROM policy_chunks" in select_sql
    assert "WHERE document_id = %s AND is_active = true" in select_sql
    assert "ORDER BY chunk_index ASC" in select_sql
    assert select_params == ("doc-a",)


def test_list_active_policy_chunks_returns_empty_list_when_no_active_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = FakePolicyChunksCursor(select_rows=[])
    conn = FakePolicyChunksConnection(cursor)
    monkeypatch.setattr(db, "_dsn", lambda: "fake-dsn")
    monkeypatch.setattr(db.psycopg, "connect", lambda *args, **kwargs: conn)

    result = db.list_active_policy_chunks("unknown-doc")

    assert result == []
