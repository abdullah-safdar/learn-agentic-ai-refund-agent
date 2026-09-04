# Stage 1.2: Automatic Resolution for Clear-Cut Requests

**Code:** [`backend/services/agent_loop.py`](../../backend/services/agent_loop.py) · [`backend/services/policy.py`](../../backend/services/policy.py) · [`backend/services/stripe_refund.py`](../../backend/services/stripe_refund.py)

## What this stage builds

[Stage 1.1](./story-1.1.md) turns a chat message into a `RefundRequest` row and stops there. This stage takes that row and actually resolves it — this is the **Agent Loop**, the part of the system that earns the word "agent." It runs three tool calls in a fixed sequence: **Order Lookup → Policy check → Stripe Refund**.

Despite the name, don't expect AI reasoning at every step here. Only [Stage 1.1's extraction call](./story-1.1.md) uses an LLM. Everything in this stage — the order lookup, the policy decision, whether to call Stripe — is plain, deterministic Python. "Agent Loop" here means *a bounded sequence of real tool calls gated by explicit decisions*, not *an LLM making every call*. The policy check specifically is a placeholder: four hardcoded `if` statements today, designed to be swapped for a real AI-driven check later ([Stage 2.2](../planning/EPICS.md)) without changing anything that calls it.

## The shape of the code

Start reading here: [`services/agent_loop.py:170`](../../backend/services/agent_loop.py#L170) — `run()`, the single entry point this whole stage revolves around. It takes a `RunInput` (today, always a fresh `NewRequestInput` — resuming a request after a human review is a later stage) and returns an `AgentResult`: `Completed`, `Escalated`, or `Failed`.

### The loop, visually

![Flowchart of the Agent Loop: a request passes through Order Lookup, amount resolution, Policy Check, and Stripe Refund. Every failure path leads to Escalated (amber); only a genuine bug leads to Failed (red, dashed); the fully clean path leads to Completed (green).](./images/stage-1.2-agent-loop.svg)

Arrow color tells you where a step leads without having to trace the line: gray goes to the next step, amber goes to `Escalated`, green goes to `Completed`, dashed red is the one path that can strike from anywhere — an unexpected bug.

**[→ Trace it interactively](https://abdullah-safdar.github.io/learn-agentic-ai-refund-agent/tutorial/stage-1.2-agent-loop-walkthrough.html)** — click through five real scenarios (happy path, order not found, policy denial, a failed Stripe call, an unexpected bug) and watch the exact path light up step by step, with a line link into `agent_loop.py` for each one. The diagram above is the quick glance; that page is the one to actually learn from.

Two things worth noticing in the shape of this diagram, not just its boxes: **`Escalated` has far more arrows pointing into it than `Failed` does** — almost everything that can go wrong routes there, because a flaky tool call or a policy "no" is a normal day, not a bug. And **the Stripe step has no retry loop drawn around it** at all, unlike Order Lookup — that missing loop is deliberate, not an oversight (see below).

### 1. Order Lookup — retried, never silently skipped

[`_resolve_order`](../../backend/services/agent_loop.py#L88) looks the order up via `db.find_order_by_reference(...)`, retrying up to `ORDER_LOOKUP_RETRY_CAP` (3) times on failure. Two things happen *before* every attempt, not after: an injection-pattern check on the customer-supplied `order_reference` (OWASP Top 10 for LLM Applications — never let something like "ignore previous instructions" reach a real tool call), and a rate-limit check. Exhausting all retries resolves to `Escalated`, **never** `Failed` — a flaky lookup is a normal thing to hand to a human, not a bug in the system.

### 2. Policy check — a stand-in with a stable shape

[`services/policy.py`](../../backend/services/policy.py)'s `evaluate_policy(order, requested_amount_cents, now)` runs four hardcoded rules: order exists and is completed, request is within a 30-day return window, requested amount is positive, and doesn't exceed the order's own amount. Called from [`agent_loop.py:156`](../../backend/services/agent_loop.py#L156). A non-compliant decision also resolves to `Escalated` — this system never autonomously tells a customer "no."

### 3. Stripe Refund — exactly once, never retried

[`_call_stripe`](../../backend/services/agent_loop.py#L115) is the one step that's deliberately *not* covered by a retry loop. Retrying a payment call after an ambiguous failure (a timeout on a charge that may have actually succeeded) risks a real double-refund, and there's no idempotency key yet to catch that (that's [Stage 1.3](../planning/EPICS.md)). One attempt, and any failure — including another injection-pattern check, this time on the customer's `reason` text, since it flows into Stripe's request metadata — escalates immediately.

## The non-obvious decision: `Failed` is not `Escalated`

Every *expected* kind of trouble — order not found, policy says no, Stripe rejects the charge, a rate limit trips — resolves to `Escalated`. `run()` only ever returns `Failed` when something violates its own contract: see the `AssertionError` guard at [`agent_loop.py:160`](../../backend/services/agent_loop.py#L160), which exists to catch a `policy.evaluate_policy()` implementation that claims `compliant=True` without a resolvable order or amount — a bug, not a business outcome. That distinction is what lets the chat API turn `Failed` into a real 502 error instead of quietly showing the customer a fake "under review" message.

## Try it yourself

```bash
cd backend
pytest tests/test_agent_loop.py   # 21 tests - orchestration + the policy rule set, including the exact return-window boundary
```

## Next stage

Stage 1.3 (not yet built) adds the idempotency key — the second layer of duplicate-refund protection this page kept pointing at. See [Epics & Stories](../planning/EPICS.md) for the full planned sequence.
