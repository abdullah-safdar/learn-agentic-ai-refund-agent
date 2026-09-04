"""FastAPI app: CORS, startup (migrations + Stripe key validation), and the
two route groups (chat, dev admin). Run with `uvicorn main:app --reload`.

Everything this module touches at import time is side-effect-free -- no
DATABASE_URL/API key is required merely to import it (for tooling, OpenAPI
generation, tests, ...). Real connections/clients are built lazily, either
in `_lifespan` (when the app actually starts) or on first use inside
db.py/llm.py/stripe_refund.py.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import db
from errors import register_error_handlers
from routes import chat as chat_api
from routes import dev as dev_api
from services import stripe_refund


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    db.run_migrations()
    # Stripe key validated at startup, not lazily on first request -- a
    # missing/invalid STRIPE_SECRET_KEY must fail at boot, matching the
    # DATABASE_URL pattern above.
    stripe_refund.validate_at_startup()
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

app.include_router(chat_api.router)
app.include_router(dev_api.router)
