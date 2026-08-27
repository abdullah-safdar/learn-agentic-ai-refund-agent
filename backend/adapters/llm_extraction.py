"""LLMPort implementation: extracts the fixed schema from chat text.

Ask First (spec): no LLM provider/SDK is architecturally mandated -- this
picks Anthropic's Claude API as a reasonable default (this project's own
tooling already targets Claude, and Anthropic's `messages.parse()` gives a
Pydantic-validated structured output in one call). Flag for renegotiation
if a different provider is preferred; swapping it out means writing a new
class against `LLMPort` and rewiring `api/main.py` -- domain/intake.py never
changes.

This adapter contains no business logic -- it only translates chat text to
the fixed `ExtractedRefundFields` schema the domain defines. Dedup, request
creation, and clarification handling all live in `domain/intake.py`.
"""

from __future__ import annotations

import os
from typing import Optional

import anthropic
from pydantic import BaseModel

from domain.ports import ExtractedRefundFields, LLMPort

# Model choice per this skill's guidance: default to Claude Opus 5 unless the
# caller overrides it (e.g. to trade accuracy for latency/cost at higher
# volume).
DEFAULT_MODEL = "claude-opus-5"

# A hanging/slow provider response must not block a chat request handler
# indefinitely -- bound it explicitly rather than relying on the SDK's
# 10-minute default.
DEFAULT_TIMEOUT_SECONDS = 12.0

_SYSTEM_PROMPT = (
    "You extract structured fields from a single customer chat message "
    "requesting a refund. Only use information the customer actually "
    "stated -- never guess or infer an order number, reason, or amount "
    "that isn't present in the message. If a field isn't stated, leave it "
    "empty (empty string for order_reference/reason, null for "
    "amount_cents). Do not attempt to validate the order or decide "
    "whether the refund is justified -- that happens elsewhere."
)


class _ExtractionSchema(BaseModel):
    """Wire schema for `messages.parse()`. Kept private to this adapter --
    the domain-facing type is `ExtractedRefundFields`."""

    order_reference: str
    reason: str
    amount_cents: Optional[int] = None


class AnthropicLLMExtractionAdapter(LLMPort):
    """LLMPort implementation backed by the Anthropic Claude API."""

    def __init__(
        self,
        client: Optional[anthropic.Anthropic] = None,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # `anthropic.Anthropic()` resolves credentials from the environment
        # (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an `ant auth login`
        # profile) -- never hardcode a key here.
        base_client = client or anthropic.Anthropic()
        # `with_options` returns a new client with the override applied --
        # it doesn't mutate `base_client` -- so this is safe even when a
        # shared client is passed in.
        self._client = base_client.with_options(timeout=timeout_seconds)
        self._model = model

    def extract_refund_request(self, chat_text: str) -> ExtractedRefundFields:
        response = self._client.messages.parse(
            model=self._model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": chat_text}],
            output_format=_ExtractionSchema,
        )
        parsed = response.parsed_output
        return ExtractedRefundFields(
            order_reference=parsed.order_reference.strip(),
            reason=parsed.reason.strip(),
            amount_cents=parsed.amount_cents,
        )


def build_default_llm_port() -> LLMPort:
    """Composition-root helper for `api/main.py`. Raises early and clearly
    if ANTHROPIC_API_KEY is missing, rather than failing on the first chat
    request."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError(
            "ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) must be set to use "
            "AnthropicLLMExtractionAdapter."
        )
    return AnthropicLLMExtractionAdapter()
