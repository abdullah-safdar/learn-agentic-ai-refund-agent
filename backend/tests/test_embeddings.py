"""Unit tests for services/embeddings.py's embed_texts() -- specifically
the count- and dimension-validation logic added after code review (a
positional zip() in services/policy_ingestion.py assumes embed_texts()
returns vectors in request order, and policy_chunks.embedding is a fixed
VECTOR(1024) column, so both must be enforced here rather than trusted or
discovered later as a cryptic Postgres error).

A fake Voyage AI client stands in for the real one -- monkeypatches
embeddings._get_client(), never a real network call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import pytest

from services import embeddings


@dataclass
class _FakeEmbeddingsResponse:
    embeddings: List[List[float]]


class _FakeVoyageClient:
    def __init__(self, response: _FakeEmbeddingsResponse) -> None:
        self._response = response
        self.calls: List[dict] = []

    def embed(self, texts: List[str], model: str, input_type: Optional[str] = None) -> _FakeEmbeddingsResponse:
        self.calls.append({"texts": texts, "model": model, "input_type": input_type})
        return self._response


def install_fake_client(monkeypatch: pytest.MonkeyPatch, response: _FakeEmbeddingsResponse) -> _FakeVoyageClient:
    client = _FakeVoyageClient(response)
    monkeypatch.setattr(embeddings, "_get_client", lambda: client)
    return client


def _vector(seed: float) -> List[float]:
    return [seed] * embeddings.EXPECTED_EMBEDDING_DIM


def test_embed_texts_returns_empty_list_for_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    # No client is even built -- an empty response object would fail
    # len(response.embeddings) != len(texts) if this short-circuit weren't first.
    assert embeddings.embed_texts([]) == []


def test_embed_texts_raises_when_response_count_does_not_match_input_count(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _FakeEmbeddingsResponse(embeddings=[_vector(1.0)])
    install_fake_client(monkeypatch, response)

    with pytest.raises(RuntimeError):
        embeddings.embed_texts(["first", "second"])


def test_embed_texts_raises_when_embedding_dimension_does_not_match_expected(monkeypatch: pytest.MonkeyPatch) -> None:
    wrong_dim_vector = [0.1, 0.2, 0.3]  # not EXPECTED_EMBEDDING_DIM
    response = _FakeEmbeddingsResponse(embeddings=[wrong_dim_vector])
    install_fake_client(monkeypatch, response)

    with pytest.raises(RuntimeError):
        embeddings.embed_texts(["only text"])


def test_embed_texts_accepts_correctly_shaped_response(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _FakeEmbeddingsResponse(embeddings=[_vector(1.0), _vector(2.0)])
    client = install_fake_client(monkeypatch, response)

    result = embeddings.embed_texts(["first", "second"])

    assert result == [_vector(1.0), _vector(2.0)]
    assert client.calls[0]["texts"] == ["first", "second"]


def test_embed_texts_always_passes_input_type_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """These texts are always policy clauses being indexed for later
    retrieval, never a search query -- Voyage's own guidance is to always
    set input_type (never leave it None) for retrieval/RAG use cases."""
    response = _FakeEmbeddingsResponse(embeddings=[_vector(1.0)])
    client = install_fake_client(monkeypatch, response)

    embeddings.embed_texts(["a policy clause"])

    assert client.calls[0]["input_type"] == "document"


def test_embed_texts_defaults_to_voyage_4_lite(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _FakeEmbeddingsResponse(embeddings=[_vector(1.0)])
    client = install_fake_client(monkeypatch, response)

    embeddings.embed_texts(["a policy clause"])

    assert client.calls[0]["model"] == "voyage-4-lite"
