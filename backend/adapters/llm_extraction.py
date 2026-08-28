"""LLMPort implementation for the OpenAI-compatible provider family.

Groq, OpenAI itself, and xAI/Grok all serve the same wire protocol (same
chat-completions request/response shape, same JSON-mode convention) -- that's
not a coincidence, they deliberately cloned OpenAI's API. Because the actual
HTTP call is identical across all of them, one generic, parameterized adapter
covers the whole family: switching provider is a `.env` change
(`LLM_PROVIDER=groq` -> `openai` -> `xai`), never a new file or a code change.

Anthropic is the one provider that does NOT belong here -- its SDK has a
genuinely different request/response shape (see `anthropic_extraction.py`).
Folding it into this file would mean branching per-provider inside one
adapter, which defeats the point of Ports & Adapters (each adapter should be
dumb translation, not a provider dispatcher) -- see
docs/planning/ARCHITECTURE-EXPLAINER.md's "How to extend this safely".

This adapter contains no business logic -- it only translates chat text to
the fixed `ExtractedRefundFields` schema the domain defines. Dedup, request
creation, and clarification handling all live in `domain/intake.py`.
"""

from __future__ import annotations

import os
from typing import Optional

from openai import OpenAI
from pydantic import BaseModel

from domain.ports import ExtractedRefundFields, LLMPort

# Mirrors the Anthropic adapter's bound -- a hanging provider response must
# not block a chat request handler indefinitely.
DEFAULT_TIMEOUT_SECONDS = 12.0

_SYSTEM_PROMPT = (
    "You extract structured fields from a single customer chat message "
    "requesting a refund. Only use information the customer actually "
    "stated -- never guess or infer an order number, reason, or amount "
    "that isn't present in the message. If a field isn't stated, leave it "
    "empty (empty string for order_reference/reason, null for "
    "amount_cents). Do not attempt to validate the order or decide "
    "whether the refund is justified -- that happens elsewhere.\n\n"
    "Respond with a single JSON object, and nothing else, matching exactly "
    "this shape:\n"
    '{"order_reference": string, "reason": string, "amount_cents": integer or null}\n'
    "amount_cents is whole cents (e.g. $12.50 -> 1250), never a float."
)

# Ask First: model IDs shift over time -- these are reasonable defaults as of
# this writing, override via LLM_MODEL if a provider's default has moved on.
# base_url values are each provider's documented OpenAI-compatible endpoint.
PROVIDER_CONFIGS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "default_model": "openai/gpt-oss-20b",
        "api_key_env_var": "GROQ_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "api_key_env_var": "OPENAI_API_KEY",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-4",
        "api_key_env_var": "XAI_API_KEY",
    },
}


class _ExtractionSchema(BaseModel):
    """Wire schema the model's JSON output is validated against. Kept
    private to this adapter -- the domain-facing type is
    `ExtractedRefundFields`."""

    order_reference: str
    reason: str
    amount_cents: Optional[int] = None


class OpenAICompatibleLLMExtractionAdapter(LLMPort):
    """LLMPort implementation for any provider serving OpenAI's chat
    completions wire protocol (Groq, OpenAI, xAI/Grok, ...). No provider
    branching lives here -- `base_url`/`model`/the API key are the only
    per-provider differences, all supplied by the caller."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_seconds)
        self._model = model

    def extract_refund_request(self, chat_text: str) -> ExtractedRefundFields:
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=1024,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": chat_text},
            ],
        )
        content = response.choices[0].message.content
        parsed = _ExtractionSchema.model_validate_json(content)
        return ExtractedRefundFields(
            order_reference=parsed.order_reference.strip(),
            reason=parsed.reason.strip(),
            amount_cents=parsed.amount_cents,
        )


def build_llm_port(provider: str) -> LLMPort:
    """Composition-root helper for `api/main.py`. `provider` must be a key
    in PROVIDER_CONFIGS. Raises early and clearly if the provider is unknown
    or its API key is missing, rather than failing on the first chat
    request."""
    config = PROVIDER_CONFIGS.get(provider)
    if config is None:
        known = ", ".join(sorted(PROVIDER_CONFIGS))
        raise RuntimeError(f"Unknown LLM_PROVIDER '{provider}'. Known providers: {known}.")

    api_key = os.environ.get(config["api_key_env_var"])
    if not api_key:
        raise RuntimeError(
            f"{config['api_key_env_var']} must be set to use provider '{provider}'."
        )

    model = os.environ.get("LLM_MODEL", config["default_model"])
    return OpenAICompatibleLLMExtractionAdapter(
        base_url=config["base_url"], api_key=api_key, model=model
    )
