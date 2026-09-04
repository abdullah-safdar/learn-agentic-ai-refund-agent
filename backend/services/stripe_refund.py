"""Issuing a refund via Stripe -- test-mode only, permanently (no real
money ever moves). No retry logic here: the caller (agent_loop.py) never
retries this step, since retrying an ambiguous-outcome failure (e.g. a
timeout on an already-successful charge) with no idempotency key yet could
double-issue a real refund.
"""

from __future__ import annotations

import os
from typing import Optional

import stripe

from models import Order

# A hanging Stripe API call must not block the Agent Loop indefinitely.
DEFAULT_TIMEOUT_SECONDS = 12.0

# Stripe metadata values are capped at 500 characters per key -- truncate
# defensively rather than letting the SDK reject the call outright over an
# overlong customer-supplied reason.
_METADATA_REASON_MAX_CHARS = 500

_client: Optional[stripe.StripeClient] = None


def _build_client(api_key: str) -> stripe.StripeClient:
    if not api_key.startswith("sk_test_"):
        raise RuntimeError(
            "STRIPE_SECRET_KEY must be a Stripe test-mode secret key (starting with "
            "'sk_test_') -- Stripe stays in test mode permanently for this project."
        )
    client = stripe.StripeClient(
        api_key,
        http_client=stripe.RequestsClient(timeout=DEFAULT_TIMEOUT_SECONDS),
        max_network_retries=0,
    )
    # A cheap, side-effect-free read-only call proves the key actually
    # authenticates against Stripe -- fail fast on a malformed/revoked key.
    try:
        client.v1.balance.retrieve()
    except Exception as exc:  # noqa: BLE001 -- any auth/connectivity failure fails boot
        raise RuntimeError(f"Stripe API key failed validation at startup: {exc}") from exc
    return client


def validate_at_startup() -> None:
    """Called once at API startup: builds and validates the Stripe client
    so a missing/invalid STRIPE_SECRET_KEY fails at boot, not on the first
    refund request."""
    global _client
    api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not api_key:
        raise RuntimeError("STRIPE_SECRET_KEY must be set to run the API.")
    _client = _build_client(api_key)


def _get_client() -> stripe.StripeClient:
    global _client
    if _client is None:
        api_key = os.environ.get("STRIPE_SECRET_KEY")
        if not api_key:
            raise RuntimeError("STRIPE_SECRET_KEY must be set to run the API.")
        _client = _build_client(api_key)
    return _client


def issue_refund(order: Order, amount_cents: int, reason: str) -> str:
    """Issue a refund for `amount_cents` against `order`. Returns the
    Stripe refund id on success. Raises on any failure -- callers must not
    retry."""
    if not order.stripe_payment_intent_id:
        raise RuntimeError(
            f"Order {order.order_reference!r} has no stripe_payment_intent_id -- "
            "cannot issue a Stripe refund against it."
        )
    client = _get_client()
    refund = client.v1.refunds.create(
        params={
            "payment_intent": order.stripe_payment_intent_id,
            "amount": amount_cents,
            "metadata": {"refund_request_reason": reason[:_METADATA_REASON_MAX_CHARS]},
        }
    )
    return refund.id
