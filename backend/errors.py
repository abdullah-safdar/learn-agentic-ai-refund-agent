"""Shared error-envelope wiring: `{ "error": { "code", "message", "details" } }`
for every error response.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from services.agent_loop import AgentLoopFailedError
from services.intake import ExtractionError

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
        # The LLM extraction call failed outright (not merely "nothing
        # stated") -- log the real exception (never returned to the
        # client) so a provider outage/error is diagnosable from server
        # logs, without leaking any secrets.
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

    @app.exception_handler(AgentLoopFailedError)
    async def _agent_loop_failed_handler(request, exc: AgentLoopFailedError) -> JSONResponse:
        # The Agent Loop resolved to AgentResult.Failed -- an unexpected/
        # internal resolution failure (a normal Tool failure escalates
        # instead). Log the real reason (never returned to the client); the
        # RefundRequest row has already been persisted as "failed" by the
        # caller before this is raised.
        logger.exception("Agent Loop resolution failed", exc_info=exc)
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "resolution_failed",
                    "message": "Couldn't resolve that refund request right now. Please try again shortly.",
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
