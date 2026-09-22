"""Turns a customer's free-text chat message into structured fields:
{order_reference, reason, amount_cents}, and (spec-2-2) judges whether a
refund request complies with the store's policy, grounded in a closed set
of retrieved Policy Store chunks. This is the one place in this codebase
that actually calls an LLM -- everything downstream/upstream of it (order
lookup, retrieval, Stripe) is plain Python or a non-LLM API call, no AI
reasoning involved.

Provider is picked via the LLM_PROVIDER env var (groq | openai | xai |
anthropic). Groq/OpenAI/xAI all speak the same OpenAI-compatible wire
protocol, so one code path handles all three; Anthropic's SDK has its own
request/response shape, so it gets its own function. Both
extract_refund_request() and judge_policy_compliance() below follow this
same provider-swap/JSON-mode/Anthropic-parse-split pattern.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List, Optional

import anthropic
from openai import OpenAI
from pydantic import BaseModel, Field

from models import ExtractedRefundFields, Order, PolicyChunk, PolicyDecision

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

_POLICY_JUDGMENT_SYSTEM_PROMPT = (
    "You are a refund policy compliance checker for an e-commerce store. "
    "You are given facts about a refund request (the order's status, order "
    "date, and total amount, the amount being requested, the customer's "
    "stated reason for the request, and today's date) and a closed set of "
    "candidate policy clauses retrieved from the store's actual refund "
    "policy documents, each tagged with its own citation_id. "
    "Decide whether the requested refund complies with the store's policy "
    "as stated in *only* the clauses given to you -- never rely on outside "
    "knowledge or general assumptions about how refunds usually work. If "
    "the given clauses don't clearly address this situation, treat that as "
    "grounds for a non-compliant decision with low confidence rather than "
    "guessing in the customer's favor. Every citation_id you return must "
    "be copied exactly from the candidate clauses below -- never invent, "
    "alter, or guess one -- and you must cite at least one clause that "
    "actually supports the decision you made."
)

_POLICY_JUDGMENT_JSON_MODE_SYSTEM_PROMPT_SUFFIX = (
    "\n\nRespond with a single JSON object, and nothing else, matching "
    "exactly this shape:\n"
    '{"compliant": boolean, "confidence": number between 0 and 1, "citation_ids": array of strings}\n'
    "Each string in citation_ids must be exactly one of the candidate clauses' own citation_id values."
)


def _format_candidate_chunks(candidate_chunks: List[PolicyChunk]) -> str:
    return "\n\n".join(f"[{chunk.citation_id}] {chunk.content}" for chunk in candidate_chunks)


def _build_policy_judgment_user_message(
    order: Order, requested_amount_cents: int, reason: str, now: datetime, candidate_chunks: List[PolicyChunk]
) -> str:
    return (
        f"Order status: {order.status}\n"
        f"Order date: {order.order_date}\n"
        f"Order total amount (cents): {order.amount_cents}\n"
        f"Requested refund amount (cents): {requested_amount_cents}\n"
        f"Reason for request: {reason}\n"
        f"Current date: {now.isoformat()}\n\n"
        f"Candidate policy clauses (cite only from these):\n{_format_candidate_chunks(candidate_chunks)}"
    )


class _PolicyJudgmentSchema(BaseModel):
    """Wire-validation schema for the OpenAI-compatible providers' JSON
    output. Kept private to this module -- the rest of the codebase only
    ever sees `PolicyDecision`. `confidence` is constrained to [0, 1] here
    so a malformed/out-of-range LLM value fails loudly (a raised
    ValidationError, propagating like any other LLM failure) rather than
    silently reaching a PolicyDecision the Escalation Threshold comparison
    would misinterpret."""

    compliant: bool
    confidence: float = Field(ge=0.0, le=1.0)
    citation_ids: List[str]


class _AnthropicPolicyJudgmentSchema(BaseModel):
    """Wire schema for Anthropic's structured-output `messages.parse()`."""

    compliant: bool
    confidence: float = Field(ge=0.0, le=1.0)
    citation_ids: List[str]


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


def _judge_policy_compliance_via_openai_compatible(
    order: Order,
    requested_amount_cents: int,
    reason: str,
    now: datetime,
    candidate_chunks: List[PolicyChunk],
    provider: str,
) -> PolicyDecision:
    client = _get_openai_compatible_client(provider)
    model = os.environ.get("LLM_MODEL", PROVIDER_CONFIGS[provider]["default_model"])
    response = client.chat.completions.create(
        model=model,
        max_tokens=1024,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": _POLICY_JUDGMENT_SYSTEM_PROMPT + _POLICY_JUDGMENT_JSON_MODE_SYSTEM_PROMPT_SUFFIX,
            },
            {
                "role": "user",
                "content": _build_policy_judgment_user_message(
                    order, requested_amount_cents, reason, now, candidate_chunks
                ),
            },
        ],
    )
    parsed = _PolicyJudgmentSchema.model_validate_json(response.choices[0].message.content)
    return PolicyDecision(
        compliant=parsed.compliant, confidence=parsed.confidence, citation_ids=list(parsed.citation_ids)
    )


def _judge_policy_compliance_via_anthropic(
    order: Order,
    requested_amount_cents: int,
    reason: str,
    now: datetime,
    candidate_chunks: List[PolicyChunk],
) -> PolicyDecision:
    client = _get_anthropic_client()
    model = os.environ.get("LLM_MODEL", ANTHROPIC_DEFAULT_MODEL)
    response = client.messages.parse(
        model=model,
        max_tokens=1024,
        system=_POLICY_JUDGMENT_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": _build_policy_judgment_user_message(
                    order, requested_amount_cents, reason, now, candidate_chunks
                ),
            }
        ],
        output_format=_AnthropicPolicyJudgmentSchema,
    )
    parsed = response.parsed_output
    return PolicyDecision(
        compliant=parsed.compliant, confidence=parsed.confidence, citation_ids=list(parsed.citation_ids)
    )


def judge_policy_compliance(
    order: Order,
    requested_amount_cents: int,
    reason: str,
    now: datetime,
    candidate_chunks: List[PolicyChunk],
) -> PolicyDecision:
    """Ask an LLM whether `requested_amount_cents` against `order` (with
    the customer's stated `reason`) complies with the store's policy,
    grounded only in `candidate_chunks` (Story 2.2's top-K retrieved Policy
    Store chunks -- services/policy.py is the only caller). Picks the
    provider from LLM_PROVIDER (default groq) on every call, same as
    extract_refund_request() above.

    The returned PolicyDecision.citation_ids reflect exactly what the LLM
    said -- this function does not filter them against `candidate_chunks`.
    That trust boundary is the caller's job (services/policy.py): an LLM
    that hallucinates or mistypes a citation_id must never have it reach a
    Trajectory unfiltered.
    """
    provider = os.environ.get("LLM_PROVIDER", "groq")
    if provider == "anthropic":
        return _judge_policy_compliance_via_anthropic(order, requested_amount_cents, reason, now, candidate_chunks)
    return _judge_policy_compliance_via_openai_compatible(
        order, requested_amount_cents, reason, now, candidate_chunks, provider
    )
