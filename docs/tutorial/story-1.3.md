# Stage 1.3: Duplicate-Safe Refunds

**Code:** [`backend/services/agent_loop.py`](../../backend/services/agent_loop.py) · [`backend/services/stripe_refund.py`](../../backend/services/stripe_refund.py)

## What this stage builds

[Stage 1.1](./story-1.1.md) and [Stage 1.2](./story-1.2.md) already protect against duplicates once — intake dedup stops a resubmitted chat message from creating a second `RefundRequest` row. But that's only half of the two-layer plan Stage 1.1 pointed at. Until now, the Stripe call itself carried no protection of its own: if `_call_stripe` were ever invoked twice for the same `RefundRequest` — a crash-then-manual-replay, a future retry path — Stripe had no way to recognize the second call as a duplicate. This stage closes that gap with a per-request **Idempotency Key**.

## The shape of the code

Start reading here: [`services/agent_loop.py:88`](../../backend/services/agent_loop.py#L88) — `_idempotency_key_for()`, a one-line function that is this entire stage's design decision. It derives a deterministic key (`f"refund-request:{refund_request.id}"`) from the `RefundRequest`'s own id — never the `Order`. That distinction matters: two different customers requesting a refund against the *same* order need two different keys, or the second, entirely legitimate request would get silently blocked by the first one's key.

[`_call_stripe`](../../backend/services/agent_loop.py#L141) generates the key and threads it straight into `stripe_refund.issue_refund(...)`. The adapter itself ([`stripe_refund.py:90`](../../backend/services/stripe_refund.py#L90)) never generates a key — it only forwards whatever it's handed, via Stripe's own `options={"idempotency_key": ...}` request option. From there, the actual duplicate-prevention work is Stripe's problem, not this codebase's: send the same key twice, and Stripe returns the original result instead of moving money again.

## The non-obvious decision: adversarial review caught a coverage gap, not a bug

The implementation was correct on the first pass — but a parallel review layer (a `verification-gap` pass, run alongside a blind hunt for missing behavior and an edge-case sweep) noticed something the others didn't: every existing test replaces `stripe_refund.issue_refund` wholesale with a fake (`monkeypatch.setattr(stripe_refund, "issue_refund", FakeStripe())`), which means the one line that actually matters — `options={"idempotency_key": idempotency_key}` reaching the real `client.v1.refunds.create(...)` call — was never exercised by anything. A regression there (the key silently dropped, or misplaced into `params` instead of `options`) would have shipped invisibly, with the whole suite still green.

[`tests/test_stripe_refund.py`](../../backend/tests/test_stripe_refund.py) fixes that: it substitutes a fake *Stripe client* one layer deeper than the other tests do, calls `issue_refund()` directly, and asserts the forwarded call actually carries the key. It's a small file, but it's the difference between "the code that generates the key is tested" and "the code that actually protects a customer's money is tested."

## Try it yourself

```bash
cd backend
pytest tests/test_agent_loop.py tests/test_stripe_refund.py   # 26 tests -- orchestration, idempotency-key generation, and the real Stripe call-site forwarding
```

## Next stage

[Stage 1.4](./story-1.4.md) makes the Agent Loop's reasoning inspectable — a trajectory log of every step it took.
