# Tutorial: Building an Agentic AI Refund Agent, Stage by Stage

This project is built as one evolving feature — a Stripe-backed customer refund chatbot — that grows through a series of stages, each adding one real agentic AI concept on top of the last. Every stage is tagged in git, so you can check out any tag and see the exact, complete, working codebase as it existed at that point.

**Planning docs** (why the project is shaped the way it is): [PRD](../planning/PRD.md) · [Architecture](../planning/ARCHITECTURE-EXPLAINER.md) · [Epics & Stories](../planning/EPICS.md)

## How to follow along

1. Pick a stage below.
2. `git checkout <tag>` to see the codebase exactly as it was at that stage — nothing from later stages included.
3. Read that stage's page here for the "why," then read the actual code.
4. Move to the next stage.

## Stages

| Stage | Tag | What it adds | Read |
|---|---|---|---|
| 1.1 | [`story-1.1-chat-intake`](https://github.com/abdullah-safdar/learn-agentic-ai-refund-agent/tree/story-1.1-chat-intake)* | A customer can submit a refund request via chat; the message is extracted into a fixed schema and intake-deduplicated | [story-1.1.md](./story-1.1.md) |
| 1.2 | [`story-1.2-agent-loop`](https://github.com/abdullah-safdar/learn-agentic-ai-refund-agent/tree/story-1.2-agent-loop) | Order lookup, policy check, and a real Stripe refund — the Agent Loop resolves clear-cut requests end-to-end | [story-1.2.md](./story-1.2.md) |

\* This tag reflects the project's original hexagonal (ports & adapters) implementation. The backend was later flattened for teaching clarity — see the "Why this doesn't look hexagonal anymore" section at the top of [story-1.1.md](./story-1.1.md) for why, and what changed. Story 1.1's actual behavior is unchanged; only its internal code structure is.

More stages are added here as they're built — see [Epics & Stories](../planning/EPICS.md) for the full planned sequence.
