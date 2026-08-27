---
stepsCompleted: [1, 2, 3, 4]
inputDocuments:
  - PRD.md
  - PRD-ADDENDUM.md
  - ARCHITECTURE-SPINE.md
  - ARCHITECTURE-EXPLAINER.md
---

# AI Payment Refund Agent - Epic Breakdown

## Overview

This document provides the complete epic and story breakdown for the AI Payment Refund Agent, decomposing the requirements from the PRD and Architecture spine into implementable stories.

## Requirements Inventory

### Functional Requirements

FR1: Customer can submit a Refund Request via chat by providing an order reference and reason in natural language, and receive a structured summary of extracted fields before the system proceeds.
FR2: Agent retrieves and cites the specific Policy clause behind every refund decision before acting (RAG-grounded, PolicyDecision{compliant, confidence, citation_ids}). Also manages conversation context length as it grows — content beyond a defined turn/token budget is summarized, never silently dropped.
FR3: Agent can choose and call a Tool (Order Lookup, Stripe Refund) as part of resolving a Refund Request, without a human specifying which API to call. Execution is bounded (a step/retry counter forces termination into completed/escalated/failed).
FR4: System prevents a retried/duplicate submission of the same Refund Request from producing two Stripe refunds, without blocking legitimate distinct refunds against the same Order. Dedup happens at intake (same logical request) and again at the point of payment (idempotency key). Also tracks memory split short-term (current conversation) versus long-term — episodic (this customer's past refund cases), semantic (policy/world knowledge), procedural (rules for how to act) — so the agent can answer "have I asked about this before?" using episodic history; this taxonomy is a named teaching objective of this stage, not incidental.
FR5: Agent exposes its multi-step Trajectory (which checks were performed, in order) for any given Refund Request, via an internal debug endpoint/UI.
FR6: System escalates to a Human Reviewer instead of acting whenever any Trajectory step's Confidence Score falls below that step's Escalation Threshold, or (once FR10 ships) the Receipt is unclear/ambiguous. Escalation is asynchronous (the loop returns cleanly and is resumed later).
FR7: A Human Reviewer can view, approve, or deny any Escalated Refund Request from an Approval Queue. MVP ships a minimal slice (no staff auth, but every decision still records a reviewer_identifier); full version adds staff authentication and multi-reviewer support.
FR8: Orchestrator routes each step of a Refund Request to the correct specialist Sub-agent (Document Verification, Policy Q&A) based on the step's nature.
FR9: System scores any completed Refund Request's Trajectory against a Golden Dataset's expected pattern and flags a mismatch (trajectory-based evals + LLM-as-judge; Golden Dataset holds 200-500 curated, never-real edge-case trajectories). Also tracks the Human Reviewer override-agreement rate over time, computed against the tentative recommendation the agent logs even when it escalates.
FR10: Customer can upload a Receipt image/PDF in place of typed order details, and the system extracts the same structured fields as FR1 (OCR/vision).

### NonFunctional Requirements

NFR1: Reliability — idempotent handling applies to every money-moving Tool call system-wide, enforced in the domain core (never in an adapter).
NFR2: Observability — every Agent Loop step across every feature is traceable via an append-only, ordered Trajectory event log; structured JSON logs are the MVP observability floor (no alerting/on-call yet).
NFR3: Cost — system aims to bound per-request LLM cost via model routing (cheap vs. strong model per step); specific cap and routing rule are open (implementation-stage decision).
NFR4: Security — OWASP Top 10 for Agentic Applications and OWASP Top 10 for LLM Applications are the system-wide baseline for every Agent Loop step; API keys/secrets never appear in logs or Trajectory rows; rate limiting enforced at the inbound API layer on both chat submissions and outbound Tool/LLM calls; domain entities and API schemas are kept distinct with an explicit field allowlist.
NFR5: Performance — no explicit latency target defined yet (open question).
NFR6: Deployment — system is deployed end-to-end for the live demo via git-push deploy (Vercel for frontend, Render for backend, Neon for Postgres); single `production` environment for MVP, no staging.
NFR7: Safety — money-moving actions above a defined dollar threshold require Human Reviewer approval regardless of Confidence Score, rigorously enforced to faithfully demonstrate the pattern (not because sandbox-mode failures carry real financial risk).
NFR8: Privacy — Receipts may contain PII; no real customer receipts or real PII may appear in the Golden Dataset, code samples, screenshots, or posts (test/synthetic only); Trajectory writes are redacted against a per-step-type field allowlist before insert, since rows can never be edited or deleted afterward; the public GitHub repo's README stays minimal.

### Additional Requirements

- No starter template mandated — the backend is hand-built to a Hexagonal/Ports & Adapters structure (`domain/`, `adapters/`, `api/`) so the Agent Loop stays framework-free by design; this structure must be established in Epic 1 Story 1.
- Stack: Python 3.13+ / FastAPI (pin at implementation time) / Pydantic v2 (not Pydantic AI) backend; React/Next.js 16.3.3+ frontend; PostgreSQL 18.x hosted on Neon (chosen specifically for its genuinely-persistent free tier); backend app hosted on Render; frontend on Vercel.
- Data model: single Postgres instance holding Order, RefundRequest (sole canonical lifecycle-status field), TrajectoryEvent (append-only, monotonic sequence_no per refund_request_id, field-allowlisted), ApprovalQueueEntry (queue/audit projection only, never authoritative).
- Agent Loop contract: exactly one entry point `run(input: RunInput) -> AgentResult`; `RunInput` is a discriminated union (`NewRequestInput` / `ResumeInput`); `AgentResult` is a discriminated union (`Completed` / `Escalated` / `Failed`) — this contract must be established before FR3 is built, since FR6's resume path and FR8's future Orchestrator both depend on it.
- Policy/Decision Port: fixed return shape `PolicyDecision{compliant, confidence, citation_ids}` from the first (MVP hardcoded-rule) implementation onward, so FR2's later RAG adapter is a swap, not a rewrite.
- Confidence Score: computed only in the domain core from raw signals returned by ports/adapters — no adapter may return a pre-computed confidence number.
- Escalation Threshold: stored as versioned/audited data (a table with effective-date/changed-by), never a hardcoded constant.
- Episodic memory (per-customer refund history) is read/written only through the Postgres Repository port, called from the domain core.
- Integration requirements: Stripe API (Refund Tool), an Order Lookup Tool, an LLM Provider API (behind an LLM Port — provider left open).
- Third-party/library note: Pydantic AI (an agent framework) is explicitly excluded — only core Pydantic (v2) is used, since hand-building the Agent Loop is the project's point.
- PRD Success Metric SM-1 (the MVP demo acceptance bar): a live, unscripted demo must handle at least 5 distinct refund scenarios — some auto-approved, at least one escalated, at least one with an unclear input — without crashing or producing a "wrong/unsafe" outcome, defined concretely as: a refund issued for a non-existent/mismatched order; a refund amount not matching the approved amount; a duplicate refund on the same request; or PII appearing in a Trajectory shown during the demo. This bar should shape MVP epic/story acceptance criteria directly.

### UX Design Requirements

None — no UX design contract exists for this project (a simple chat interface plus a staff approval-queue list; no dedicated UX spec was produced).

### FR Coverage Map

FR1: Epic 1 - Chat-based refund submission, core intake for end-to-end resolution
FR2: Epic 2 - Policy-cited decisions replace the MVP hardcoded rule set (RAG)
FR3: Epic 1 - Autonomous tool selection, the Agent Loop's core act step
FR4: Epic 1 - Idempotent refund handling and episodic memory
FR5: Epic 1 - Inspectable reasoning trajectory
FR6: Epic 1 - Confidence-gated escalation
FR7: Epic 1 - Staff approval workflow (MVP minimal slice)
FR8: Epic 4 - Specialist routing via a multi-agent Orchestrator
FR9: Epic 5 - Trajectory scoring against a golden dataset
FR10: Epic 3 - Receipt upload and extraction

## Epic List

### Epic 1: End-to-End Refund Resolution
Customers can get a refund request resolved completely — automatically for clear-cut cases, or routed to a human reviewer for ambiguous ones — with every step of the agent's reasoning inspectable afterward. This is the full MVP demo bar (PRD SM-1).
**FRs covered:** FR1, FR3, FR4, FR5, FR6, FR7

### Epic 2: Policy-Grounded Decisions
The agent's approve/deny decisions are grounded in the store's actual refund policy documents, with citations — replacing the MVP's hardcoded rule set via the already-pluggable Policy Port (AD-6).
**FRs covered:** FR2

### Epic 3: Receipt-Based Refund Requests
Customers can submit a photo or PDF of their receipt instead of typing order details, with unclear extractions routed to a human.
**FRs covered:** FR10

### Epic 4: Multi-Agent Orchestration
Complex requests get routed to specialist sub-agents (document verification, policy Q&A) instead of one general-purpose agent handling everything.
**FRs covered:** FR8

### Epic 5: Trajectory Evaluation & Quality Tracking
The team can measure whether the agent's decisions are actually good against a golden dataset, and track reviewer-override trends over time.
**FRs covered:** FR9

## Epic 1: End-to-End Refund Resolution

Customers can get a refund request resolved completely — automatically for clear-cut cases, or routed to a human reviewer for ambiguous ones — with every step of the agent's reasoning inspectable afterward. This is the full MVP demo bar (PRD SM-1).

### Story 1.1: Submit a Refund Request via Chat

As a customer,
I want to describe my refund request in chat,
So that the system understands what I need without me filling out a form.

**Acceptance Criteria:**

**Given** a customer sends a chat message naming an order reference and a reason
**When** the system processes it
**Then** it extracts `{amount, order_reference}` in the fixed schema (FR-1)
**And** the customer sees a confirmation summary of the extracted fields before anything proceeds

**Given** a `RefundRequest` has already been created from a near-identical recent message (same order reference, normalized reason, within the intake dedup time window)
**When** a second such message arrives
**Then** the domain reuses the existing `RefundRequest` row rather than creating a duplicate (AD-3 intake dedup)

**Given** inbound chat submissions
**When** the rate limit configured at the API adapter layer is exceeded
**Then** further submissions are rejected rather than passed through (AD-11 rate limiting)

### Story 1.2: Automatic Resolution for Clear-Cut Requests

As a customer,
I want a valid refund request to be resolved automatically,
So that I don't have to wait on a human for the obvious cases.

**Acceptance Criteria:**

**Given** an extracted, non-duplicate refund request
**When** the Agent Loop runs via `run(NewRequestInput(...))`
**Then** it calls the Order Lookup Tool, then the Policy/Decision Port (a minimal hardcoded rule-set adapter checking: order exists and is completed, request is within the return window, amount matches — AD-6), and only if `PolicyDecision.compliant` is true does it call the Stripe Refund Tool and return `AgentResult.Completed{refund_id, amount_cents}` (AD-2)

**Given** a Tool call fails (network error, timeout)
**When** the Agent Loop's Observe step evaluates the failure
**Then** it retries up to the domain-enforced step/retry cap (AD-10) before escalating or returning `AgentResult.Failed` — it never retries indefinitely

**Given** agent-constructed input destined for a Tool call
**When** that Tool call is built
**Then** the input is checked against injection patterns first, and outbound Tool/LLM calls are subject to the configured rate limit (AD-11) — both before the call is made

### Story 1.3: Duplicate-Safe Refunds

As a customer,
I want retrying my request (a double-click, a dropped connection) to never charge or refund me twice,
So that a network hiccup doesn't cost real money.

**Acceptance Criteria:**

**Given** a `RefundRequest` that has already succeeded
**When** the same request is resubmitted or its Stripe call is retried
**Then** no second Stripe refund is issued, enforced via a per-request Idempotency Key checked and recorded in the domain core before any Stripe call (AD-3)

**Given** a second, distinct legitimate refund request against the same Order (e.g. a different item)
**When** it is processed
**Then** it is not blocked by the first request's Idempotency Key

### Story 1.4: Inspect the Agent's Reasoning

As the builder,
I want to see every step the agent took for a given request,
So that I can debug and demonstrate how it reached its decision.

**Acceptance Criteria:**

**Given** a completed or in-progress Refund Request
**When** the debug endpoint/UI is opened for it
**Then** its `TrajectoryEvent` rows are shown in order by `sequence_no` (AD-5), each populated only with its step type's allowlisted fields

**Given** a step involved a raw Tool payload or Receipt-derived data
**When** that step's `TrajectoryEvent` is written
**Then** the payload is redacted/summarized to the allowlist before insert — never stored as a free-form blob

### Story 1.5: Escalate When Uncertain

As a customer,
I want an unclear or borderline request to go to a real person instead of being guessed at,
So that mistakes don't get automated.

**Acceptance Criteria:**

**Given** a request where the domain-computed Confidence Score (AD-8) falls below the Escalation Threshold (AD-9)
**When** the Agent Loop evaluates it
**Then** it returns `AgentResult.Escalated{pending_review_id, tentative_recommendation}` instead of acting, and persists the `pending_review` state

**Given** any refund amount at or above the configured dollar threshold
**When** the Agent Loop considers auto-approving it
**Then** it always escalates regardless of Confidence Score

**Given** the Policy/Decision Port returns `compliant: false` for a request
**When** the Agent Loop evaluates the result
**Then** it escalates rather than auto-denying — the agent never autonomously rejects a request, matching the "auto-approve or defer to a human" design (never an autonomous "no")

### Story 1.6: Resolve Escalated Requests as Staff

As a staff reviewer,
I want to see escalated requests and approve or deny them,
So that the customer still gets a resolution without the agent guessing.

**Acceptance Criteria:**

**Given** an Escalated Refund Request
**When** a reviewer opens the Approval Queue and approves or denies it
**Then** the decision plus a `reviewer_identifier` (unauthenticated in MVP, per AD-7/Consistency Conventions) is recorded, and the Agent Loop resumes via `run(ResumeInput(refund_request_id, reviewer_decision))` to completion (AD-4)

**Given** a reviewer decision has been recorded
**When** any other part of the system checks whether the request is approved
**Then** it reads `RefundRequest.status` — never `ApprovalQueueEntry` — as authoritative (AD-7)

### Story 1.7: Deploy the System for Live Demo

As the builder,
I want the full system running on real infrastructure,
So that I can run the SM-1 live demo instead of only on localhost.

**Acceptance Criteria:**

**Given** working backend, frontend, and database code
**When** deployment is configured
**Then** the frontend is live on Vercel, the backend on Render, and the database on Neon (per the architecture's chosen stack), each reachable via a public URL, with secrets supplied via environment variables — never hardcoded (Consistency Conventions)

**Given** the deployed system
**When** a full request flows end-to-end (chat submission through to a Stripe test-mode call)
**Then** it completes successfully in the deployed environment, not just locally

## Epic 2: Policy-Grounded Decisions

The agent's approve/deny decisions are grounded in the store's actual refund policy documents, with citations — replacing the MVP's hardcoded rule set via the already-pluggable Policy Port (AD-6).

### Story 2.1: Ingest the Store's Refund Policy Documents

As the builder,
I want the store's refund policy documents embedded and stored,
So that the agent has a searchable, current copy of the actual policy to check against.

**Acceptance Criteria:**

**Given** a set of refund policy documents
**When** the ingestion pipeline runs
**Then** the documents are chunked and embedded into the Policy Store, each chunk retrievable by a stable `citation_id`

**Given** a policy document is updated
**When** re-ingestion runs
**Then** the Policy Store reflects the updated content without leaving stale chunks live

### Story 2.2: Policy-Cited Compliance Checking Replaces the Hardcoded Rule Set

As a customer,
I want my refund evaluated against the store's real policy, not a placeholder rule,
So that the decision reflects actual store rules, including edge cases the simple rules missed.

**Acceptance Criteria:**

**Given** a Refund Request being evaluated
**When** the Policy/Decision Port is invoked
**Then** the RAG-backed adapter returns `PolicyDecision{compliant, confidence, citation_ids}` with at least one non-empty `citation_id`, replacing the MVP hardcoded adapter (AD-6) — no caller code changes, since both adapters share the same port shape

**Given** a policy-based decision was made
**When** the decision is logged to the Trajectory (Story 1.4)
**Then** the cited `citation_ids` are included in that step's allowlisted fields

### Story 2.3: Manage Growing Conversation Context

As the builder,
I want long conversations summarized instead of silently truncated,
So that the agent doesn't lose track of earlier context or blow past token limits.

**Acceptance Criteria:**

**Given** a conversation whose context exceeds the configured turn/token budget
**When** the next agent step runs
**Then** older context is summarized (not silently dropped) before being passed to the LLM

**Given** a summarized conversation
**When** the customer references something from earlier
**Then** the agent still has access to the substance of that context via the summary

## Epic 3: Receipt-Based Refund Requests

Customers can submit a photo or PDF of their receipt instead of typing order details, with unclear extractions routed to a human.

### Story 3.1: Submit a Refund Request via Receipt Upload

As a customer,
I want to upload a photo or PDF of my receipt instead of typing my order details,
So that I don't have to look up and retype information I already have on paper.

**Acceptance Criteria:**

**Given** a customer uploads a Receipt image or PDF in chat
**When** the system processes it
**Then** it extracts the same fixed schema fields (`amount`, `order_reference`) as Story 1.1's text path (FR-10), feeding the same intake flow

**Given** the extracted Receipt data
**When** it is persisted or logged
**Then** it passes through AD-12's field allowlist and AD-5's redaction — raw receipt content never lands unredacted in a `TrajectoryEvent` or API response

### Story 3.2: Escalate Uncertain Receipt Extractions

As a customer,
I want a blurry or ambiguous receipt to be reviewed by a person rather than misread,
So that a bad OCR read doesn't cause a wrong refund.

**Acceptance Criteria:**

**Given** a Receipt extraction with low confidence or unresolvable fields
**When** the Agent Loop evaluates it
**Then** it escalates (Story 1.5's mechanism) rather than guessing at the missing or unclear fields

## Epic 4: Multi-Agent Orchestration

Complex requests get routed to specialist sub-agents (document verification, policy Q&A) instead of one general-purpose agent handling everything.

### Story 4.1: Orchestrator Routes Document Verification to a Specialist Sub-agent

As the builder,
I want document-verification steps handled by a dedicated specialist Sub-agent,
So that the system's reasoning is modular instead of one agent handling every kind of task.

**Acceptance Criteria:**

**Given** a Refund Request whose next step is document verification
**When** the Orchestrator evaluates it
**Then** it routes that step to a Document Verification Sub-agent, itself a wrapped instance of the same Agent Loop (`run()`/`AgentResult` contract, AD-2)

**Given** the Sub-agent completes its step
**When** its contribution is logged
**Then** it appears as a distinctly labeled step (tagged with the Sub-agent's name) within the shared Trajectory (AD-5)

### Story 4.2: Orchestrator Routes Policy Questions to a Specialist Sub-agent

As the builder,
I want policy-related questions handled by a dedicated specialist Sub-agent,
So that policy reasoning is modular and reuses the same orchestration mechanism as document verification.

**Acceptance Criteria:**

**Given** a Refund Request whose next step is a policy question
**When** the Orchestrator evaluates it
**Then** it routes that step to a Policy Q&A Sub-agent using the same routing mechanism established in Story 4.1

**Given** multiple Sub-agents contribute to one Refund Request
**When** its Trajectory is viewed
**Then** each Sub-agent's steps are distinguishable from the others' by name, in correct `sequence_no` order

## Epic 5: Trajectory Evaluation & Quality Tracking

The team can measure whether the agent's decisions are actually good against a golden dataset, and track reviewer-override trends over time.

### Story 5.1: Score Trajectories Against a Golden Dataset

As the builder,
I want completed request Trajectories scored against a curated golden dataset,
So that I can tell whether the agent is actually making good decisions, not just plausible-looking ones.

**Acceptance Criteria:**

**Given** a Golden Dataset of 200-500 curated edge-case Trajectories (synthetic, never real customer data — Privacy constraint)
**When** a new Refund Request's Trajectory is scored against it
**Then** the system flags a mismatch using trajectory-based comparison plus LLM-as-judge scoring, not just final-answer comparison

**Given** a scored Trajectory
**When** results are reviewed
**Then** the score references which step(s) diverged from the expected pattern, not just a pass/fail verdict

### Story 5.2: Track Reviewer Override-Agreement Over Time

As the builder,
I want to see whether human reviewers agree or disagree with the agent's tentative recommendations over time,
So that I can tell if the agent's judgment is improving, stagnant, or getting worse.

**Acceptance Criteria:**

**Given** a Human Reviewer resolves an Escalated Refund Request (Story 1.6)
**When** their decision is recorded
**Then** it is compared against the agent's logged tentative recommendation (FR-6/FR-9) and the agreement/disagreement outcome is stored

**Given** a history of override-agreement outcomes
**When** the metric is viewed
**Then** the trend is visible over time, not just as a single current-moment number — supporting SM-C1's requirement to catch Escalation Threshold gaming after the fact
