"""Unit tests for `services.stripe_refund.issue_refund()` itself -- these
exercise the actual `client.v1.refunds.create(...)` call site (with a fake
Stripe client substituted in), rather than monkeypatching
`issue_refund` away wholesale the way `test_agent_loop.py` and
`test_intake.py` do. Without this, a regression on the idempotency-key
forwarding at that exact call site (dropped, misplaced, mis-keyed) would
ship undetected while the rest of the suite stays green -- see
spec-1-3-duplicate-safe-refunds.md.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from models import Order
from services import stripe_refund


class FakeRefund:
    def __init__(self, refund_id: str) -> None:
        self.id = refund_id


class FakeRefundsResource:
    """Records every `create(...)` call's args so tests can assert exactly
    what was forwarded to Stripe -- including `options`."""

    def __init__(self, refund_id: str = "re_fake123") -> None:
        self._refund_id = refund_id
        self.calls: List[Dict[str, Any]] = []

    def create(self, params: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> FakeRefund:
        self.calls.append({"params": params, "options": options})
        return FakeRefund(self._refund_id)


class FakeV1:
    def __init__(self, refunds: FakeRefundsResource) -> None:
        self.refunds = refunds


class FakeStripeClient:
    def __init__(self, refund_id: str = "re_fake123") -> None:
        self.refunds = FakeRefundsResource(refund_id=refund_id)
        self.v1 = FakeV1(self.refunds)


def make_order(
    order_reference: str = "ORD-1234",
    stripe_payment_intent_id: Optional[str] = "pi_fake123",
) -> Order:
    return Order(
        id="order-1",
        order_reference=order_reference,
        status="completed",
        amount_cents=5000,
        order_date="2026-08-01T00:00:00Z",
        stripe_payment_intent_id=stripe_payment_intent_id,
    )


def test_issue_refund_forwards_idempotency_key_via_options(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakeStripeClient(refund_id="re_forwarded")
    monkeypatch.setattr(stripe_refund, "_get_client", lambda: fake_client)
    order = make_order()

    refund_id = stripe_refund.issue_refund(
        order=order,
        amount_cents=1500,
        reason="wrong size",
        idempotency_key="refund-request:some-id",
    )

    assert refund_id == "re_forwarded"
    assert len(fake_client.refunds.calls) == 1
    call = fake_client.refunds.calls[0]
    assert call["options"] == {"idempotency_key": "refund-request:some-id"}
    assert call["params"]["payment_intent"] == "pi_fake123"
    assert call["params"]["amount"] == 1500


def test_issue_refund_passes_distinct_idempotency_keys_through_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = FakeStripeClient()
    monkeypatch.setattr(stripe_refund, "_get_client", lambda: fake_client)
    order = make_order()

    stripe_refund.issue_refund(order=order, amount_cents=1000, reason="r1", idempotency_key="refund-request:a")
    stripe_refund.issue_refund(order=order, amount_cents=1000, reason="r2", idempotency_key="refund-request:b")

    keys = [call["options"]["idempotency_key"] for call in fake_client.refunds.calls]
    assert keys == ["refund-request:a", "refund-request:b"]
