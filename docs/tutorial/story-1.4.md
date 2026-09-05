# Stage 1.4: Inspect the Agent's Reasoning

**Code:** [`backend/services/agent_loop.py`](../../backend/services/agent_loop.py) · [`backend/db.py`](../../backend/db.py) · [`backend/routes/trajectory.py`](../../backend/routes/trajectory.py) · [`frontend/app/trajectory/[id]/page.tsx`](../../frontend/app/trajectory/%5Bid%5D/page.tsx)

## What this stage builds

By [Stage 1.3](./story-1.3.md), the Agent Loop resolves a refund end-to-end — but it leaves no trace of *how*. If a request escalates, all you can see is `status: escalated`. You can't tell whether the order was never found, the policy check failed, or Stripe itself rejected the call. That's fine for a demo where everything works, but useless the moment something doesn't.

This stage adds a **Trajectory**: an append-only log of every step the Agent Loop took for a given refund request, in order, readable afterward via a debug endpoint (and now a small page in the frontend). Think of it as a black-box flight recorder for one request — not a general-purpose logging system, a structured record of *this specific agent's reasoning*.

## The shape of the code

Four kinds of steps get recorded, one row each, in the order they actually happened:

| Step | Recorded from | What it captures |
|---|---|---|
| `order_lookup` | [`_run_new_request`](../../backend/services/agent_loop.py#L220) | Whether an order was found, and how many retries it took |
| `policy_decision` | [`_run_new_request`](../../backend/services/agent_loop.py#L241) | The compliance decision, confidence score, and policy citations |
| `stripe_refund` | [`_run_new_request`](../../backend/services/agent_loop.py#L273) | Whether the refund completed, and the resulting Stripe refund id |
| `outcome` | [`run`](../../backend/services/agent_loop.py#L278) | The final result: `completed`, `escalated`, or `failed` |

Start reading here: [`_record_step`](../../backend/services/agent_loop.py#L98) — a small function every one of those four call sites goes through. It builds a small dictionary (`step_data`) and hands it to [`db.record_trajectory_event`](../../backend/db.py#L221), which is the only place that actually writes to Postgres.

Two design choices worth noticing in that dictionary:

- **It's never the raw thing.** `_run_new_request` doesn't hand `_record_step` an exception, a Stripe payload, or the customer's original message — it hands over a small, deliberately chosen set of fields (`found`, `compliant`, `outcome`, ...). This is the field allowlist: each step type has a *fixed* shape, decided once, and nothing outside that shape is ever written. That's what makes a Trajectory safe to expose later through a debug endpoint — there's no raw data to accidentally leak.
- **The sequence number is assigned by the database, not by the caller.** [`record_trajectory_event`](../../backend/db.py#L221) locks any existing rows for this refund request (`SELECT ... FOR UPDATE`), computes `MAX(sequence_no) + 1`, and inserts — all inside one transaction. Nobody upstream ever says "this is step 3"; the write itself decides that, which is what keeps the ordering trustworthy even if something about the calling code changes later.

Reading it back is the easy half: [`GET /api/refund-requests/{id}/trajectory`](../../backend/routes/trajectory.py#L48) just lists the rows for that id, oldest first, through a response model ([`TrajectoryEventSummary`](../../backend/routes/trajectory.py#L19)) that only ever exposes the same four allowlisted fields — never the internal `TrajectoryEvent` row directly.

The frontend piece is a thin read: [`app/trajectory/[id]/page.tsx`](../../frontend/app/trajectory/%5Bid%5D/page.tsx) fetches that endpoint and renders the steps as a simple timeline of cards, plus a "🔍 Trajectory" link from the dev dashboard's refund-requests table. Nothing here changes the Agent Loop — it's a viewer, not a new decision.

Here's what a completed request actually looks like once it's resolved — four cards, oldest first, each showing only that step's allowlisted fields:

![Screenshot of the Refund Request Trajectory page: four cards in order — Order Lookup (Found: Yes, Retries Used: 0), Policy Decision (Compliant: Yes, Confidence: 1), Stripe Refund (Outcome: completed, Refund Id, Amount Cents: $34.99), and Outcome (Status: completed) — each timestamped, with the order reference and a green "completed" badge in the page header.](./images/stage-1.4-trajectory-viewer.png)

Notice what's *not* there: no customer message, no raw Stripe response, no stack trace. Just the four allowlisted fields per step — proof that the redaction rule from the previous section isn't just a design intention, it's what actually reaches the screen.

## The non-obvious decision: an observability write must never win against the real result

Here's the mistake worth studying, because it's the kind that's easy to make and easy to miss: the first working version of `_record_step` called `db.record_trajectory_event` with no protection around it. That looks harmless — it's "just" a logging call. But look at where it's called from:

```python
stripe_result = _call_stripe(refund_request, order, requested_amount_cents, rate_limiter)
# ... _record_step(..., stripe_step_data, now) happens here, AFTER Stripe already succeeded
return stripe_result
```

If Stripe has *already issued the refund* and the trajectory write then fails for any reason — a database hiccup, a network blip, a rare race on the sequence number — that exception propagates upward. `run()`'s own `try/except` catches it and converts it into `Failed`. The customer would see an error message for a refund that had, in fact, already gone through. The record-keeping broke the thing it was only supposed to be watching.

This is exactly the kind of bug an adversarial code review is good at catching — and did, here, independently, from three different review angles at once. The fix ([`_record_step`, line 98](../../backend/services/agent_loop.py#L98)) is one `try/except`:

```python
try:
    db.record_trajectory_event(refund_request_id, step_type, step_data, now)
except Exception:
    logger.warning("Failed to record TrajectoryEvent for ...", exc_info=True)
```

Log it, swallow it, move on. The rule this teaches generalizes well beyond this project: **anything whose only job is to observe a result must never be able to change that result.** A metrics call, an audit log, an analytics ping — if writing it can fail, that failure needs to be strictly less important than the thing it's describing. Design the write path so "the log entry didn't save" and "the customer's refund broke" can never become the same event.

## Try it yourself

```bash
cd backend
pytest tests/test_agent_loop.py tests/test_trajectory_route.py   # trajectory recording + the endpoint
pytest tests/                                                     # 55 tests -- full suite, Stages 1.1-1.4
```

```bash
cd frontend
npm run dev   # then visit /dashboard, submit a refund via /chat, click "🔍 Trajectory" on it
```

Manual check without the frontend at all:

```bash
curl http://localhost:8000/api/refund-requests/<id>/trajectory
```

## Next stage

[Stage 1.5](./story-1.5.md) adds a real Escalation Threshold and Confidence Score, so the agent decides *when* to hand a request to a human instead of guessing.
