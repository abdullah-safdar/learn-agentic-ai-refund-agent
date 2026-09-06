# Stage 1.5: Escalate When Uncertain

**Code:** [`backend/services/agent_loop.py`](../../backend/services/agent_loop.py) · [`backend/db.py`](../../backend/db.py) · [`backend/models.py`](../../backend/models.py)

## What this stage builds

By [Stage 1.4](./story-1.4.md), the Agent Loop already escalates when something goes *wrong* — an order it can't find, a policy it can't satisfy. But every request that passed those checks got auto-approved, no matter how large the refund or how confident the decision actually was. This stage adds two new reasons to hand a request to a human instead of auto-approving it:

- **Too large.** Any refund at or above a configured dollar amount always escalates, regardless of confidence.
- **Not confident enough.** If the decision's confidence score falls below a configured threshold, it escalates instead of guessing.

Both cutoffs live in a new **Escalation Threshold**, stored in Postgres rather than hardcoded — so they're audited data, not a constant buried in code.

## The shape of the code

Start reading here: [`_run_new_request`, line 262](../../backend/services/agent_loop.py#L262) — right after the policy check passes and the order/amount are resolved, but *before* the Stripe call. That placement is the whole design: nothing past this point can move money until both checks clear.

```python
threshold = db.get_current_escalation_threshold(now)
...
if (
    requested_amount_cents >= threshold.dollar_threshold_cents
    or decision.confidence < threshold.confidence_threshold
):
    return _escalated_for(refund_request)
```

Two things worth noticing in that condition:

- **Either check alone is enough.** A $10 refund with low confidence escalates. A $10,000 refund at maximum confidence *also* escalates — the dollar cutoff always wins, exactly per the story's acceptance criteria.
- **The confidence score isn't new.** It's the same `PolicyDecision.confidence` field the hardcoded policy check has returned since Stage 1.2 — always `1.0` today, since that check is a deterministic yes/no, not a probabilistic judgment. Wiring the comparison now means this path is already correct and ready for whenever a real policy adapter (Epic 2) starts returning varying confidence.

The threshold itself is a small, append-only table ([`db.py:71`](../../backend/db.py#L71)) — `confidence_threshold`, `dollar_threshold_cents`, plus `effective_at`/`changed_by` for an audit trail. Nothing updates a row in place; a future change would insert a new one. [`get_current_escalation_threshold`](../../backend/db.py#L371) always reads whichever row is currently in effect — the latest one whose `effective_at` isn't in the future.

## The non-obvious decision: read failures must fail *safe*, not fail *open*

What happens if reading the threshold itself breaks — a database hiccup, a bad connection? The tempting shortcut is to let it raise and become `Failed`, since that's what unexpected errors already do elsewhere in this function. But `Failed` isn't safe here: it's not "we couldn't decide," it's "an unreadable threshold quietly got skipped and the request fell through to Stripe anyway" if the exception were handled wrong, or a confusing customer-facing error if it weren't handled at all.

The actual rule, at [line 263](../../backend/services/agent_loop.py#L263):

```python
try:
    threshold = db.get_current_escalation_threshold(now)
except Exception:
    logger.warning("Failed to read EscalationThreshold for refund_request_id=%r -- escalating", ...)
    return _escalated_for(refund_request)
```

An unreadable threshold escalates, full stop — logged, never raised, never auto-approved. It's the same "never guess" principle the whole epic is built on, just applied one layer earlier: if the system can't even find out what the rules are, the safe default is a human, not a coin flip.

A second, smaller version of the same instinct showed up during review: the first draft read the threshold using Postgres's own wall-clock (`now()`), while every other time-sensitive call in this file threads an explicit `now` parameter for determinism (`record_trajectory_event` does this too — see Stage 1.4). Left alone, that inconsistency would have made the threshold check impossible to test deterministically and, in theory, able to drift from the `now` the rest of the request was reasoned about. The fix ([`db.py:371`](../../backend/db.py#L371)) threads the same injected `now` through, matching the convention everywhere else.

## Try it yourself

```bash
cd backend
pytest tests/test_agent_loop.py   # amount-threshold, confidence-threshold, and read-failure escalation tests
pytest tests/                     # 60 tests -- full suite, Stages 1.1-1.5
```

Manual check — lower the dollar threshold and watch a refund escalate that would otherwise auto-approve:

```sql
INSERT INTO escalation_thresholds (id, confidence_threshold, dollar_threshold_cents, effective_at, changed_by)
VALUES (gen_random_uuid(), 0.7, 100, now(), 'manual-test');
```

## Next stage

[Stage 1.6](./story-1.6.md) lets a staff reviewer see escalated requests and approve or deny them, resuming the Agent Loop to completion.
