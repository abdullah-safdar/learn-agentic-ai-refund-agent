"""Composition root: wires concrete adapters to the domain ports and exposes
the FastAPI app referenced by `uvicorn api.main:app`.

Not called out in the spec's Code Map by filename, but required for the
spec's own Verification command (`uvicorn api.main:app --reload`) to have
anything to run -- this is the minimal glue, not a new architectural piece.

Adapter construction (`_get_repository`/`_get_llm_port`) is lazy: merely
importing this module (for tooling, OpenAPI generation, tests, ...) must not
require `DATABASE_URL`/`ANTHROPIC_API_KEY` to be set or create a live
Anthropic client as a side effect. Construction happens on first use --
either the migrations step in `_lifespan` (when the app actually starts) or
the first request that resolves the corresponding dependency.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from adapters.llm_extraction import build_default_llm_port
from adapters.postgres_repository import PostgresRefundRepository
from api import chat_routes
from api.errors import register_error_handlers
from domain.ports import LLMPort

_repository: Optional[PostgresRefundRepository] = None
_llm_port: Optional[LLMPort] = None


def _get_repository() -> PostgresRefundRepository:
    global _repository
    if _repository is None:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL must be set to run the API.")
        _repository = PostgresRefundRepository(dsn=dsn)
    return _repository


def _get_llm_port() -> LLMPort:
    global _llm_port
    if _llm_port is None:
        _llm_port = build_default_llm_port()
    return _llm_port


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    _get_repository().run_migrations()
    yield


app = FastAPI(title="AI Payment Refund Agent -- Chat Intake API", lifespan=_lifespan)
register_error_handlers(app)

# CORS: the frontend (Next.js dev server locally, Vercel in production) is
# always a different origin from this API (Render), so browser requests
# need explicit CORS allowance. Configurable via env var rather than
# hardcoded so the allowed origin(s) can differ per environment.
_allowed_origins = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.dependency_overrides[chat_routes.get_llm_port] = _get_llm_port
app.dependency_overrides[chat_routes.get_repository_port] = _get_repository

app.include_router(chat_routes.router)
