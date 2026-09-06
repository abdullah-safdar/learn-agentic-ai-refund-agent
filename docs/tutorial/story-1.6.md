# Stage 1.6: Resolve Escalated Requests as Staff

**Code:** [`backend/routes/approvals.py`](../../backend/routes/approvals.py) · [`backend/db.py`](../../backend/db.py) · [`backend/services/agent_loop.py`](../../backend/services/agent_loop.py) · [`backend/models.py`](../../backend/models.py) · [`frontend/app/approvals/page.tsx`](../../frontend/app/approvals/page.tsx)

## What this stage builds

By [Stage 1.5](./story-1.5.md), the Agent Loop escalates a request instead of guessing — but escalating and then doing nothing with it isn't a resolution, it's a dead end. This stage closes the loop: a staff reviewer can see every Escalated request in an **Approval Queue** and approve or deny it, and that decision resumes the *same* Agent Loop through to a real outcome instead of leaving the customer stuck at "someone will look at this."

The queue is deliberately unauthenticated in this MVP (per AD-7/Consistency Conventions — there's no staff identity system yet), so a reviewer just types a name or staff id before deciding. Approve re-attempts the refund through Stripe (and can still re-escalate if the order's gone missing in the meantime); deny closes the case immediately, no Stripe call at all.

## The shape of the code

`run()`'s signature already had a `ResumeInput` branch reserved for this since Stage 1.5's `NewRequestInput`/`ResumeInput` union — it just raised `NotImplementedError`. This stage wires it up: [`_resume_request`](../../backend/services/agent_loop.py#L306), a sibling to `_run_new_request` that shares the same `_resolve_order`/`_call_stripe` machinery but skips the policy check and Escalation Threshold entirely — a human already overrode those by approving.

```python
if reviewer_decision != "approve":
    return Denied(refund_request_id=refund_request_id)

lookup_succeeded, order, attempts = _resolve_order(refund_request, rate_limiter)
...
stripe_result = _call_stripe(refund_request, order, requested_amount_cents, rate_limiter)
return stripe_result
```

`Denied` is a new `AgentResult` variant ([`models.py`](../../backend/models.py)) — a real terminal business outcome, distinct from `Failed` (an unexpected internal error) and `Completed` (money actually moved). The route layer, [`routes/approvals.py`](../../backend/routes/approvals.py), is a thin HTTP shell around two things: `GET /api/approvals/refund-requests` lists everything `status = "escalated"` ([`db.list_escalated_refund_requests`](../../backend/db.py#L446), oldest first), and `POST .../decision` records the decision, then calls `agent_loop.run(ResumeInput(...))` — the exact same entry point `chat.py`'s new-request path already uses, just resumed instead of started fresh.

The frontend piece ([`app/approvals/page.tsx`](../../frontend/app/approvals/page.tsx)) is a table with Approve/Deny buttons per row, plus a link into Stage 1.4's Trajectory viewer so a reviewer can see *why* something escalated before deciding:

![Screenshot of the Approval Queue page: a table of escalated refund requests, each row showing an order reference, reason, amount, and escalation timestamp, with "Trajectory", "Deny", and "Approve" buttons. A "Your name or staff id" field above the table is filled in with "priya.reviewer". The bottom row shows order ORD-9001, reason "the item arrived damaged.", amount $750.00, escalated just now.](./images/stage-1.6-approval-queue.png)

## The non-obvious decision: two reviewers, one request, must not both win

Nothing stops two staff members from opening the same escalated request at once — there's no locking in the UI, no "someone else is reviewing this" indicator. So what happens if both click "Approve" within the same second?

The unsafe version would read the request, check `status == "escalated"`, and write the decision as three separate steps — leaving a window between the check and the write where a second decision can slip through and the Agent Loop runs `_resume_request` twice for the same refund, potentially calling Stripe twice.

The actual fix lives in [`db.record_reviewer_decision`](../../backend/db.py#L465): the status transition and the audit-trail insert happen as one atomic SQL statement pair inside a single transaction, and the transition itself is guarded by the same `WHERE` clause it's reading:

```python
cur.execute(
    "UPDATE refund_requests SET status = %s WHERE id = %s AND status = %s",
    (interim_status, refund_request_id, STATUS_ESCALATED),
)
if cur.rowcount != 1:
    raise ConcurrentDecisionError(...)
```

If a row was already flipped out of `escalated` by another decision, `rowcount` comes back `0` and this raises instead of silently proceeding — no `ApprovalQueueEntry` gets inserted, and `routes/approvals.py` translates the exception into a `409 Conflict` before `agent_loop.run()` is ever called. The second reviewer sees an honest "this was already decided," not a silent double-refund.

It's the same shape of bug Stage 1.5 fixed for reading the Escalation Threshold — a gap between "check" and "act" that only shows up under concurrency — just here the fix is a database-level guard instead of a try/except, because the database is the one thing both requests actually share.

## Try it yourself

```bash
cd backend
pytest tests/test_approvals_route.py tests/test_agent_loop.py   # the new route + resume-through-run() tests
pytest tests/                                                    # 79 tests -- full suite, Stages 1.1-1.6
```

```bash
cd frontend
npm run dev   # then visit /chat, submit a refund large enough to escalate (over $500), then /approvals
```

Manual check without the frontend at all:

```bash
curl http://localhost:8000/api/approvals/refund-requests

curl -X POST http://localhost:8000/api/approvals/refund-requests/<id>/decision \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve", "reviewer_identifier": "your-name"}'
```

## Next stage

Stage 1.7 (not yet built) deploys the system to real infrastructure — Vercel, Render, and Neon — so the demo runs somewhere other than localhost. See [Epics & Stories](../planning/EPICS.md) for the full planned sequence.
