# Stage 1.1: Submit a Refund Request via Chat

**Tag:** `story-1.1-chat-intake` · **Code:** [`backend/`](../../backend/) · [`frontend/app/chat/`](../../frontend/app/chat/)

## What this stage builds

Before any of this project can call itself "agentic," it needs the most basic thing an agent needs: a way to turn a messy human sentence into something a program can act on. This stage does exactly that, and nothing more — no order lookup, no policy check, no Stripe call. Just: a customer types a message, an LLM extracts the useful fields from it, and the system remembers it without creating duplicates.

## The shape of the code

This is also where the project's whole backend structure gets established — **Hexagonal / Ports & Adapters**. The idea: the actual decision-making logic (`domain/`) never knows or cares whether it's talking to a real LLM, a fake one in a test, Postgres, or something else entirely. It only talks to interfaces (*ports*) that it defines itself.

```
backend/
  domain/     # the core logic - no imports from adapters/ or api/, ever
  adapters/   # concrete implementations: Anthropic API, Postgres
  api/        # the FastAPI HTTP layer - just another adapter
```

Start reading here: [`domain/intake.py:112`](../../backend/domain/intake.py#L112) — `submit_chat_message`, the single function this entire stage is built around. Everything else exists to serve it.

## The non-obvious decision: two-layer duplicate protection

If you only think about "don't double-refund someone," you'd reach for an idempotency key on the Stripe call and stop there. That's necessary, but it's not sufficient — it only protects a request that's *already been created* from being processed twice. It does nothing about the customer accidentally sending the same request twice in the first place (a double-click, a flaky connection retrying the chat message).

So there are two separate layers, both living in `domain/`, never in an adapter:

1. **Intake dedup** — before a `RefundRequest` row even gets created, check whether an equivalent one already exists (same order, same reason, recent). See [`intake.py:87`](../../backend/domain/intake.py#L87).
2. **Idempotency key** — a second, independent safety net on the money-moving call itself. See [`ports.py:61`](../../backend/domain/ports.py#L61).

One without the other leaves a real gap — which is exactly what an adversarial code review caught after the first implementation pass: a collision on the dedup key could return a *locally built* refund request instead of the one that actually got persisted. See [`intake.py:69`](../../backend/domain/intake.py#L69) for the fix.

## Try it yourself

```bash
cd backend
pip install -r requirements.txt
pytest tests/test_intake.py   # 10 tests - the full I/O matrix plus review-driven regressions
```

## Next stage

Stage 1.2 takes this extracted request and actually resolves it — looking up the order, checking policy, and calling Stripe.
