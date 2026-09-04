"""Turns a customer's free-text chat message into structured fields:
{order_reference, reason, amount_cents}. This is the one place in this
codebase that actually calls an LLM -- everything downstream (order lookup,
policy, Stripe) is plain Python, no AI involved.

Provider is picked via the LLM_PROVIDER env var (groq | openai | xai |
anthropic). Groq/OpenAI/xAI all speak the same OpenAI-compatible wire
protocol, so one code path handles all three; Anthropic's SDK has its own
request/response shape, so it gets its own function.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

import anthropic
from openai import OpenAI
from pydantic import BaseModel

from models import ExtractedRefundFields

# A hanging provider response must not block a chat request handler
# indefinitely.
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

_JSON_MODE_SYSTEM_PROMPT_SUFFIX = (
    "\n\nRespond with a single JSON object, and nothing else, matching "
    "exactly this shape:\n"
    '{"order_reference": string, "reason": string, "amount_cents": integer or null}\n'
    "amount_cents is whole cents (e.g. $12.50 -> 1250), never a float."
)

# Model IDs shift over time -- these are reasonable defaults as of this
# writing; override via LLM_MODEL if a provider's default has moved on.
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

ANTHROPIC_DEFAULT_MODEL = "claude-opus-5"


class _ExtractionSchema(BaseModel):
    """Wire-validation schema for the OpenAI-compatible providers' JSON
    output. Kept private to this module -- the rest of the codebase only
    ever sees `ExtractedRefundFields`."""

    order_reference: str
    reason: str
    amount_cents: Optional[int] = None


class _AnthropicExtractionSchema(BaseModel):
    """Wire schema for Anthropic's structured-output `messages.parse()`."""

    order_reference: str
    reason: str
    amount_cents: Optional[int] = None


# Lazily-built, cached per provider so importing this module never requires
# an API key to be set -- only actually calling extract_refund_request()
# does.
_openai_compatible_clients: Dict[str, OpenAI] = {}
_anthropic_client: Optional[anthropic.Anthropic] = None


def _get_openai_compatible_client(provider: str) -> OpenAI:
    if provider not in _openai_compatible_clients:
        config = PROVIDER_CONFIGS.get(provider)
        if config is None:
            known = ", ".join(sorted(PROVIDER_CONFIGS))
            raise RuntimeError(f"Unknown LLM_PROVIDER '{provider}'. Known providers: {known}.")
        api_key = os.environ.get(config["api_key_env_var"])
        if not api_key:
            raise RuntimeError(f"{config['api_key_env_var']} must be set to use provider '{provider}'.")
        _openai_compatible_clients[provider] = OpenAI(
            base_url=config["base_url"], api_key=api_key, timeout=DEFAULT_TIMEOUT_SECONDS
        )
    return _openai_compatible_clients[provider]


def _get_anthropic_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise RuntimeError(
                "ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) must be set to use LLM_PROVIDER=anthropic."
            )
        _anthropic_client = anthropic.Anthropic().with_options(timeout=DEFAULT_TIMEOUT_SECONDS)
    return _anthropic_client


def _extract_via_openai_compatible(chat_text: str, provider: str) -> ExtractedRefundFields:
    client = _get_openai_compatible_client(provider)
    model = os.environ.get("LLM_MODEL", PROVIDER_CONFIGS[provider]["default_model"])
    response = client.chat.completions.create(
        model=model,
        max_tokens=1024,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT + _JSON_MODE_SYSTEM_PROMPT_SUFFIX},
            {"role": "user", "content": chat_text},
        ],
    )
    parsed = _ExtractionSchema.model_validate_json(response.choices[0].message.content)
    return ExtractedRefundFields(
        order_reference=parsed.order_reference.strip(),
        reason=parsed.reason.strip(),
        amount_cents=parsed.amount_cents,
    )


def _extract_via_anthropic(chat_text: str) -> ExtractedRefundFields:
    client = _get_anthropic_client()
    model = os.environ.get("LLM_MODEL", ANTHROPIC_DEFAULT_MODEL)
    response = client.messages.parse(
        model=model,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": chat_text}],
        output_format=_AnthropicExtractionSchema,
    )
    parsed = response.parsed_output
    return ExtractedRefundFields(
        order_reference=parsed.order_reference.strip(),
        reason=parsed.reason.strip(),
        amount_cents=parsed.amount_cents,
    )


def extract_refund_request(chat_text: str) -> ExtractedRefundFields:
    """Extract {order_reference, reason, amount_cents} from a single
    customer chat message. Picks the provider from LLM_PROVIDER (default
    groq) on every call -- switching providers is only ever an env var
    change."""
    provider = os.environ.get("LLM_PROVIDER", "groq")
    if provider == "anthropic":
        return _extract_via_anthropic(chat_text)
    return _extract_via_openai_compatible(chat_text, provider)
