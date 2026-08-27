---
title: AI Payment Refund Agent - PRD
status: final
created: 2026-08-27
updated: 2026-08-27
---

# PRD: AI Payment Refund Agent
*Working title — confirm.*

## 0. Document Purpose

This PRD defines the AI Payment Refund Agent, a staged agentic-AI learning/portfolio project. It is written for geni (solo builder) to drive downstream architecture and story-breakdown work. Terms are defined once in the Glossary (§3) and used consistently; functional requirements are grouped under features (§4) and numbered globally; inferred details are tagged `[ASSUMPTION]` and indexed in §8. This PRD builds on the prior brainstorming session's artifacts (`build-roadmap.md`, `content-notes.md`) rather than duplicating them — deep technical implementation detail and the LinkedIn content strategy live there, not here.

## 1. Vision

This project is a Stripe-backed customer refund chatbot for a fictional e-commerce store — built not as a single demo, but as one feature evolving through 12 stages, from a bare LLM API call to a production-grade agentic system with real guardrails, evaluation, and multi-agent orchestration.

Its true purpose isn't the refund chatbot itself — it's proof, for geni, an aspiring full-stack AI developer with two named weak spots (agent-specific skills: tool calling, memory, planning, guardrails; and production judgment: cost, latency, reliability, deployment). Payments force both, in a way a "toy" agent demo can't.

Built and shared stage-by-stage in a public LinkedIn series, it exists to make one claim uncontestable to a hiring manager: this person can take an agentic system from a bare API call to something they'd actually trust with real money.

## 2. Target User

### 2.1 Jobs To Be Done

- *(for geni)* Prove hands-on competency in agent-specific skills and production judgment through one coherent working system, not disconnected exercises
- *(for geni)* Generate a natural, staged narrative for the public build-in-public content series
- *(for geni, emotional)* Move from "self-taught, unproven" to "demonstrably competent" before entering the job market
- *(in-fiction customer)* Get a clear-cut refund resolved fast without waiting on a human; get routed to a human fast when it's genuinely unclear

### 2.2 Non-Users (v1)

- Real payment processing — Stripe stays in test/sandbox mode; no real money ever moves
- Other businesses looking for a shippable refund product — this is a demo built to prove skill, not a product for sale

### 2.3 Key User Journeys

*Kept light — hobby/solo scope.*

- **UJ-1.** A customer with a clear, policy-compliant refund request submits a receipt in chat and gets an automatic refund confirmation in the same conversation.
- **UJ-2.** A customer with an unclear/ambiguous document gets told a human will review it; a staff member sees it in an approval queue and resolves it.

*Form factor: web-based chat interface (confirmed).*

## 3. Glossary

- **Refund Request** — A customer-initiated request to reverse a payment, tied to an Order and a submitted Receipt.
- **Order** — The original e-commerce transaction record a Refund Request is checked against.
- **Receipt** — Evidence document (image/PDF) the customer submits to support a Refund Request.
- **Policy** — Internal, versioned refund rules stored in the Policy Store.
- **Policy Store** — RAG-embedded refund-policy documents queried at decision time.
- **Confidence Score** — The agent's self-assessed certainty for a given decision step, normalized to 0–1. The computation mechanism is a proposed default — see `PRD-ADDENDUM.md`. `[ASSUMPTION: not yet confirmed — see §8]`
- **Escalation** — Routing a Refund Request to the Approval Queue instead of the agent deciding alone.
- **Escalation Threshold** — The Confidence Score floor below which Escalation is triggered.
- **Approval Queue** — The staff-facing interface listing Escalated Refund Requests.
- **Human Reviewer** — Staff role permitted to approve/deny an Escalated Refund Request.
- **Agent Loop** — The perceive → decide → act → observe cycle executed per Refund Request.
- **Tool** — An external capability the Agent Loop can invoke (for example, Stripe Refund API, Order Lookup).
- **Orchestrator** — The top-level agent that routes a Refund Request to the correct specialist Sub-agent.
- **Sub-agent** — A specialist agent (for example, Document Verification, Policy Q&A) coordinated by the Orchestrator.
- **Idempotency Key** — A unique identifier attached to a money-moving Tool call to guarantee a retry cannot double-refund.
- **Trajectory** — The full sequence of Agent Loop steps taken for one Refund Request; the unit of Evaluation.
- **Golden Dataset** — The curated set of edge-case Trajectories (never real customer data) used to Evaluate agent quality.
- **Guardrail** — A rule or check (aligned to OWASP Top 10 for Agentic Applications and OWASP Top 10 for LLM Applications) constraining Agent Loop behavior.

## 4. Features

### 4.1 Conversational Refund Intake
**Description:** Customer chats in natural language and submits a Refund Request; system extracts structured fields and can execute a basic Stripe refund call. Text-based order reference input is the core (Must) path; Receipt image/PDF upload with OCR/vision extraction is a separate, later enhancement (FR-10). Realizes UJ-1, UJ-2.

#### FR-1: Chat-based refund submission
Customer can submit a Refund Request via chat by providing an order reference and reason in natural language, and receive a structured summary of extracted fields before the system proceeds.

**Consequences (testable):**
- System returns extracted fields (amount, order reference) in a fixed schema for every accepted submission.
- System calls the Stripe refund API only after fields are extracted and validated against an Order.

**Out of Scope:** Receipt image/PDF upload (see FR-10); multi-currency (see §5 Non-Goals).

### 4.2 Policy-Grounded Reasoning (RAG)
**Description:** Agent answers and decides using an embedded store of the business's own refund Policy documents, and manages conversation context length as it grows. Realizes UJ-1.

#### FR-2: Policy-cited decisions with context management
Agent retrieves and cites the specific Policy clause behind every refund decision before acting.

**Consequences (testable):**
- Every decision references at least one retrieved Policy chunk ID.
- Conversation context beyond a defined turn/token budget is summarized, never silently dropped. `[ASSUMPTION: specific budget value not yet set — see §8]`

### 4.3 Agentic Decision & Action Loop
**Description:** Replaces one-shot prompting with a real Agent Loop that autonomously calls Tools. Realizes UJ-1.

#### FR-3: Autonomous tool selection and use
Agent can choose and call a Tool (Order Lookup, Stripe Refund) as part of resolving a Refund Request, without a human specifying which API to call.

**Consequences (testable):**
- Every Refund Request produces a visible Trajectory naming the Tool(s) called, in order.
- A failed Tool call triggers an Observe step that decides retry, replan, or Escalation — never a silent failure. After a bounded number of failed retries or a timeout, the Agent Loop escalates rather than retrying further (never a non-terminating loop). `[ASSUMPTION: default retry cap/timeout not yet confirmed — see §8]`
- Agent Loop exposes a single well-defined entry/exit contract so a future Orchestrator (FR-8) can wrap it as a Sub-agent without a rewrite.

### 4.4 Memory & Duplicate Prevention
**Description:** Tracks short-term conversation state and long-term per-customer refund history; guarantees a retried request can't produce two refunds for one Order. Realizes UJ-1.

#### FR-4: Idempotent refund handling
System prevents a retried/duplicate submission of the same Refund Request from producing two Stripe refunds, without blocking legitimate distinct refunds against the same Order (for example, separate partial refunds for different items). Memory is split short-term (current conversation/context window) versus long-term — episodic (this customer's past refund cases), semantic (policy/world knowledge), procedural (rules for how to act) — per the named memory taxonomy this stage is meant to teach.

**Consequences (testable):**
- Every money-moving Tool call carries an Idempotency Key that uniquely distinguishes distinct legitimate requests against the same Order (never derived from the Order alone) — see `PRD-ADDENDUM.md` for the derivation formula.
- Agent can answer "have I asked about this before?" using stored episodic (per-customer) history, distinct from its semantic policy knowledge.

### 4.5 Multi-Step Planning
**Description:** Chains identity check → order check → policy check → amount decision → refund action as a visible reasoning Trajectory. Realizes UJ-1.

#### FR-5: Inspectable reasoning trajectory
Agent exposes its multi-step Trajectory (which checks were performed, in order) for any given Refund Request.

**Consequences (testable):**
- Trajectory is retrievable via an internal debug endpoint/UI listing steps in order for any completed Refund Request — not just a raw log dump.

### 4.6 Guardrails & Security
**Description:** Gates money movement behind confidence-scored Escalation and approval, defended against OWASP's Agentic and LLM Top 10 risks (prompt injection, excessive agency, memory poisoning). Realizes UJ-2.

#### FR-6: Confidence-gated escalation
System escalates to a Human Reviewer instead of acting whenever any Trajectory step's Confidence Score falls below that step's Escalation Threshold, or (once FR-10 ships) the Receipt is unclear/ambiguous.

**MVP-specific scoping** (before FR-2 (RAG) and FR-10 (Receipt-upload) exist):
- Policy-compliance is evaluated via a minimal hardcoded rule set, superseded entirely once FR-2 ships — see `PRD-ADDENDUM.md` for the specific rules.
- Escalation triggers on any of the following: any step's Confidence Score below its threshold, an order reference/reason that cannot be resolved or verified, or FR-3's retry cap being exhausted. The Receipt-unclear trigger applies only once FR-10 ships.

**Consequences (testable):**
- No refund above `[ASSUMPTION: a specific dollar threshold — needs a value, see §8]` is issued without Human Reviewer approval, regardless of Confidence Score.
- Agent input is checked against injection patterns before being used to construct any Tool call.
- Agent logs a tentative recommendation even when it escalates, so a Human Reviewer's decision has something concrete to agree with or override (feeds FR-9).

**Feature-specific NFRs:** Guardrail behavior aligned to OWASP Top 10 for Agentic Applications (ASI01–10) and OWASP Top 10 for LLM Applications.

### 4.7 Human-Review UX
**Description:** Provides a staff-facing Approval Queue with authenticated roles for resolving Escalated requests. Realizes UJ-2.

#### FR-7: Staff approval workflow
A Human Reviewer can view, approve, or deny any Escalated Refund Request from an authenticated Approval Queue.

**Consequences (testable):**
- *(MVP minimal, §6.1):* Approval Queue is accessible without staff authentication; Reviewer decision (approve/deny and an optional note) is logged against the Refund Request.
- *(Full version, post-MVP, §6.2):* Approval Queue requires staff authentication distinct from the customer-facing chat, with support for multiple named reviewers.

### 4.8 Multi-Agent Orchestration
**Description:** An Orchestrator routes work to specialist Sub-agents (Document Verification, Policy Q&A) instead of one monolithic agent handling everything. `[NOTE FOR PM: this pattern carries outsized architectural weight — the same orchestrator-managing-worker-loop shape recurred independently three separate times during brainstorming, arguably the single highest-leverage concept in the whole curriculum, even though it's Should-tier for MVP timing. FR-3's orchestrator-readiness consequence is the actual enforcement point for this note — that's where it must be honored during MVP build, not here.]`

#### FR-8: Specialist routing
Orchestrator routes each step of a Refund Request to the correct specialist Sub-agent based on the step's nature.

**Consequences (testable):**
- Each Sub-agent's contribution appears as a distinctly labeled step (tagged with the Sub-agent's name) within the shared Trajectory record.

### 4.9 Evaluation & Continuous Improvement
**Description:** Measures agent quality against a Golden Dataset using Trajectory-based scoring and LLM-as-judge, and tracks whether Human Reviewer overrides agree with the agent's own decision.

#### FR-9: Trajectory scoring against a golden dataset
System scores any completed Refund Request's Trajectory against the Golden Dataset's expected pattern and flags a mismatch.

**Consequences (testable):**
- Golden Dataset contains 200–500 curated edge-case Trajectories (never real customer data — see Privacy; generation methodology in `PRD-ADDENDUM.md`).
- Human Reviewer override-agreement rate is tracked over time as a running metric, computed against the tentative recommendation the agent logs even on Escalation (FR-6), so it's well-defined for every reviewed case.

### 4.10 Multimodal Document Understanding
**Description:** Extends Feature 4.1 so a customer can submit a Receipt (image/PDF) instead of typing an order reference; system extracts structured fields via OCR/vision. Realizes UJ-1.

#### FR-10: Receipt upload and extraction
Customer can upload a Receipt image/PDF in place of typed order details, and the system extracts the same structured fields as FR-1.

**Consequences (testable):**
- Unclear/low-confidence extraction routes to Escalation (FR-6) rather than guessing.

## 5. Non-Goals (Explicit)

- This will not become a multi-tenant SaaS product sold to other businesses.
- No real payments are ever processed — Stripe stays in test/sandbox mode permanently, even after "completion."
- Not building a general-purpose agent framework to compete with LangGraph/CrewAI/MCP — ecosystem awareness is study-only (see §8), not a build target.
- Not pursuing formal compliance certification (PCI-DSS, SOC 2) — out of scope given sandbox-only payments.
- Not supporting multiple businesses/tenants, multiple currencies, or multiple languages in v1.

## 6. MVP Scope

### 6.1 In Scope (Must)
- FR-1 (Chat-based refund submission), FR-3 (Autonomous tool selection), FR-4 (Idempotent refund handling), FR-5 (Inspectable reasoning trajectory), FR-6 (Confidence-gated escalation)
- FR-7, minimal slice only (Human-Review UX) — a bare-bones list of Escalated requests with approve/deny action; no staff roles/permissions polish yet. Pulled into Must so SM-1's demo can show an escalation resolved end-to-end, not just triggered. `[DECISION: confirmed by geni]`
- Cross-Cutting NFRs: Reliability, Security, Observability (core logging only)

### 6.2 Out of Scope for MVP
- FR-2 (Policy-Grounded Reasoning, RAG) — *Should*, first thing added after MVP
- FR-7, full version (staff authentication/roles, multi-reviewer workflow) — deferred; MVP ships the minimal slice only (see §6.1)
- FR-8 (Multi-Agent Orchestration) — *Should*, deferred as a "v2" architectural story
- FR-9 (Evaluation & Continuous Improvement) — *Could*
- FR-10 (Multimodal Document Understanding) — *Could*
- Full cost and observability tooling (model routing, tracing dashboards) — *Must-tier conceptually per the roadmap, but deferred past a minimal version for MVP* `[NOTE FOR PM: roadmap marked this Must; MVP here only takes the minimal slice — revisit if the full version is load-bearing for the demo]`
- Ecosystem Awareness (Stage 12) — Won't-this-time; study only, never a build target

## Cross-Cutting NFRs

- **Reliability:** Idempotent handling (FR-4) applies to every money-moving Tool call system-wide, not just one feature path.
- **Observability:** Every Agent Loop step across every feature is traceable via its Trajectory (ties to FR-3, FR-5, FR-9).
- **Cost:** LLM API usage is geni's personal expense during development. System aims to bound per-request cost via model routing (cheaper model for simple steps, stronger model for complex reasoning); the specific step-classification rule and model tiers are an architecture-stage decision, not specified here. `[ASSUMPTION: specific cost cap and routing rule not yet defined — see §8]`
- **Security:** OWASP Top 10 for Agentic Applications and OWASP Top 10 for LLM Applications are the system-wide baseline, not just Feature 4.6's local guardrail — applies to every Agent Loop step. API keys/secrets never appear in logs or Trajectories. System enforces rate limiting on inbound chat requests and outbound Tool and LLM calls (defends against the OWASP "unbounded consumption" risk).
- **Performance:** `[ASSUMPTION: no explicit latency target defined yet — see §8]`
- **Deployment:** System is deployed somewhere runnable end-to-end for the live demo (SM-1) via a basic CI/CD pipeline. `[ASSUMPTION: specific hosting target and pipeline not yet chosen — see §8]`

## Constraints and Guardrails

**Safety**
- This project's money-movement guardrail (FR-6) exists to correctly demonstrate the pattern for hiring/portfolio evaluation purposes — not because sandbox-mode failures carry real financial risk. It should be built with the same rigor as if real money were at stake, since that rigor is the entire point of this project (§1).

**Privacy**
- Receipts may contain PII (names, card fragments, addresses). `[NOTE FOR PM: since this project is built in public — LinkedIn posts, and code shared via a GitHub repo — no real customer receipts or real PII may appear in the Golden Dataset, code samples, screenshots, or posts. Test/synthetic receipts only.]`
- The public GitHub repo's README stays minimal (a project intro and a guide to run the code); deeper build rationale and all content-strategy material stay local, never published.

## 7. Success Metrics

**Primary**
- **SM-1**: Live, unscripted demo handles at least five distinct refund scenarios — some auto-approved, at least one escalated, at least one with an unclear input — without crashing or producing a wrong/unsafe outcome. "Wrong/unsafe" means, concretely: a refund issued for an order that doesn't exist or doesn't match; a refund amount that doesn't match the approved amount; a duplicate refund on the same request; PII appearing in a Trajectory shown during the demo. Anything outside this list is a judgment call, not an automatic fail. Validates FR-1, FR-3–FR-6.
- **SM-2**: Stages 1–11 each ship a working code sample and a LinkedIn post; Stage 12 (Ecosystem Awareness) ships a study artifact (format per Open Question 4) and its own post, not code, since Stage 12 is explicitly never a build target (§5). Validates the full roadmap.

**Secondary**
- **SM-3**: Once FR-9 exists, the agent's Trajectory matches the expected pattern on at least 80% of the Golden Dataset. If FR-9 is never built, SM-3 is void — not failed. Validates FR-9.

**Counter-metrics (do not optimize)**
- **SM-C1**: Auto-approval rate must never be pushed up by loosening the Escalation Threshold — a wrongly auto-approved refund is worse than an unnecessary human escalation. Escalation Threshold changes and the resulting auto-approval rate are logged over time, so this is checkable after the fact rather than a self-reported principle. Counterbalances FR-6/SM-1.

## 8. Open Questions & Assumptions

*Merged: every unresolved item, whether first raised as a question or an inline `[ASSUMPTION]` tag, tracked once.*

1. FR-6's dollar threshold for mandatory Human Reviewer approval — no specific value defined yet. `[ASSUMPTION]`
2. Cross-Cutting Cost NFR — no specific LLM spending cap or model-routing classification rule defined yet. `[ASSUMPTION]`
3. Cross-Cutting Performance NFR — no explicit latency target defined yet. `[ASSUMPTION]`
4. Ecosystem Awareness (Stage 12 / non-goal) — no defined format yet for how this "study, don't build" output gets captured (notes? a short comparison doc?) — low priority, revisit if time allows.
5. Deployment target and CI/CD pipeline — not yet chosen; needed before SM-1's live demo can happen. `[ASSUMPTION]`
6. FR-3's default retry cap/timeout value — proposed as a concept, no specific number confirmed yet. `[ASSUMPTION]`
7. Confidence Score's default computation mechanism (see `PRD-ADDENDUM.md`) — proposed default, not yet confirmed; architecture stage should validate or replace it. `[ASSUMPTION]`
8. FR-2's context summarization turn/token budget — no value set yet. `[ASSUMPTION]`
9. Form factor is web-based chat — **confirmed by geni, no longer open.**

~~Phase-blocker: SM-1 required demoing an escalation, but FR-7 was scoped out of MVP~~ — **Resolved**: a minimal FR-7 slice (approve/deny list, no roles) pulled into Must scope (§6.1).
