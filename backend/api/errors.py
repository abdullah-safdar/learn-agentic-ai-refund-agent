"""Shared error-envelope wiring: `{ "error": { "code", "message", "details" } }`
for every error response, per the architecture's Consistency Conventions.

Factored out of `api/main.py` so tests can register the exact same handlers
on a test-only FastAPI app and verify the real response shape (e.g. the
rate-limit 429 in `tests/test_intake.py`), instead of relying on FastAPI's
default `{"detail": ...}` wrapping.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from domain.intake import ExtractionError

logger = logging.getLogger(__name__)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request body failed validation.",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": "http_error", "message": str(exc.detail), "details": None}},
        )

    @app.exception_handler(ExtractionError)
    async def _extraction_error_handler(request, exc: ExtractionError) -> JSONResponse:
        # The LLM Port failed outright (not merely "nothing stated") -- a
        # customer-visible clarification prompt isn't right here since we
        # never got extraction output at all. Clean envelope, not a raw 500.
        # Log the real exception (never returned to the client) so a
        # provider outage/error is diagnosable from server logs -- per
        # AD-11, this must never include secrets, only the exception itself.
        logger.exception("Refund chat extraction failed", exc_info=exc)
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "extraction_failed",
                    "message": "Couldn't process that message right now. Please try again shortly.",
                    "details": None,
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception while handling request", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": "Something went wrong.", "details": None}},
        )
