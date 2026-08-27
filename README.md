# Learn Agentic AI: Refund Agent (Built From Scratch)

A Stripe-backed customer refund chatbot, built as one feature evolving through a series of stages — from a bare LLM API call to a production-grade agentic system with real guardrails, evaluation, and multi-agent orchestration. No agent framework (LangGraph, CrewAI, etc.) — the agent loop is hand-built, on purpose, so every piece is actually understood rather than configured.

📖 **Follow it as a tutorial:** [`docs/tutorial/`](docs/tutorial/) — one stage per tag, each with a full working codebase and an explanation of what it adds and why.

📐 **Planning docs:** [PRD](docs/planning/PRD.md) · [Architecture](docs/planning/ARCHITECTURE-EXPLAINER.md) · [Epics & Stories](docs/planning/EPICS.md)

## Stack

Python / FastAPI / Pydantic v2 backend, React / Next.js frontend, PostgreSQL. Stripe runs in test/sandbox mode only — no real payments are ever processed.

## Running it locally

**Backend:**
```bash
cd backend
pip install -r requirements.txt
cp .env.example .env   # fill in DATABASE_URL and ANTHROPIC_API_KEY
uvicorn api.main:app --reload
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

**Tests:**
```bash
cd backend
pytest tests/
```
