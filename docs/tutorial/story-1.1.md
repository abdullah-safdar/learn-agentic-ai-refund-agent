# Stage 1.1: Submit a Refund Request via Chat

**Tag:** [`story-1.1-chat-intake`](https://github.com/abdullah-safdar/learn-agentic-ai-refund-agent/tree/story-1.1-chat-intake) (original pass — see the note below) · **Code:** [`backend/`](../../backend/) · [`frontend/app/chat/`](../../frontend/app/chat/)

## Why this doesn't look hexagonal anymore

If you check out the tag above, you'll see something different from what's described on this page: `domain/`, `adapters/`, `api/` folders, abstract base classes, a `Depends()`-driven wiring layer. That was the original pass — proper Ports & Adapters, the enterprise-standard way to structure a backend.

It got ripped out. Not because it was wrong, but because it was the wrong lesson for this series. This project teaches agentic AI to people who are often new to Python *at the same time* — a real chunk of the audience is coming from JavaScript. Stack "here's an abstract base class," "here's a dependency-injection container," and "here's how an AI agent decides things" all in one sitting, and the actual lesson — the AI part — gets buried under architecture vocabulary that has nothing to do with it.

So the backend was flattened: plain functions, called directly by name, organized into a couple of familiar folders (`routes/`, `services/`) instead of a layered ports-and-adapters structure. Tests moved too — from fake classes that subclassed an interface, to `pytest`'s `monkeypatch.setattr(module, "fn_name", fake)`. If you've used `jest.spyOn()` or `jest.mock()`, that's the exact same move.

The `story-1.1-chat-intake` tag is kept as an honest record of the original attempt — not deleted, not rewritten. This page describes the current, simplified version on `main`.

## What this stage builds

Before any of this project can call itself "agentic," it needs the most basic thing an agent needs: a way to turn a messy human sentence into something a program can act on. This stage does exactly that, and nothing more — no order lookup, no policy check, no Stripe call yet (that's [Stage 1.2](./story-1.2.md)). Just: a customer types a message, an LLM extracts the useful fields from it, and the system remembers it without creating duplicates.

## The shape of the code

```
backend/
  main.py            # FastAPI app, CORS, startup (migrations + Stripe key check)
  models.py           # data shapes: dataclasses + Union result types
  db.py                # Postgres access, plain functions
  errors.py            # error-envelope wiring
  rate_limit.py         # fixed-window rate limiter
  routes/
    chat.py             # POST /api/chat/refund-requests
    dev.py               # dev-only seeding/inspection endpoints
  services/
    intake.py            # this stage's code
    agent_loop.py          # Stage 1.2
    policy.py
    llm.py
    stripe_refund.py
    dev_admin.py
```

Start reading here: [`services/intake.py:91`](../../backend/services/intake.py#L91) — `submit_chat_message`, the single function this entire stage is built around. Everything else exists to serve it. It calls `llm.extract_refund_request(...)` ([`services/llm.py`](../../backend/services/llm.py)) to turn the raw chat text into `{order_reference, reason, amount_cents}`, then hands dedup + persistence off to `db.py`.

## The non-obvious decision: two-layer duplicate protection

If you only think about "don't double-refund someone," you'd reach for an idempotency key on the Stripe call and stop there. That's necessary, but it's not sufficient — it only protects a request that's *already been created* from being processed twice. It does nothing about the customer accidentally sending the same request twice in the first place (a double-click, a flaky connection retrying the chat message).

So there are two separate layers:

1. **Intake dedup** (built here, Stage 1.1) — before a `RefundRequest` row even gets created, check whether an equivalent one already exists (same order, same normalized reason, recent time window). See [`compute_dedup_key`](../../backend/services/intake.py#L66) and the check-then-create flow starting at [`submit_chat_message`](../../backend/services/intake.py#L91).
2. **Idempotency key** — a second, independent safety net directly on the money-moving Stripe call. See [Stage 1.3](./story-1.3.md).

One without the other leaves a real gap — which is exactly what an adversarial code review caught after the first implementation pass: a collision on the dedup key could return a *locally built* refund request instead of the one that actually got persisted. See the race-handling branch starting at [`services/intake.py:124`](../../backend/services/intake.py#L124) for the fix — it re-fetches and returns the row that actually won, never the one that got silently discarded.

## Try it yourself

```bash
cd backend
pip install -r requirements.txt
pytest tests/test_intake.py   # 15 tests - the full I/O matrix plus review-driven regressions
```

## Next stage

[Stage 1.2](./story-1.2.md) takes this extracted request and actually resolves it — looking up the order, checking policy, and calling Stripe.
