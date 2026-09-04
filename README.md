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
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in DATABASE_URL, GROQ_API_KEY, STRIPE_SECRET_KEY
uvicorn main:app --reload --env-file .env
```

**Getting the API keys** for `.env`:

- **Groq** (free, default LLM provider — used to extract refund requests from chat):
  1. Go to [console.groq.com/keys](https://console.groq.com/keys) and sign in (or create a free account).
  2. Click **Create API Key**, name it, and copy the value (starts with `gsk_...`).
  3. Paste it into `.env` as `GROQ_API_KEY=gsk_...`. Leave `LLM_PROVIDER=groq` as-is.

- **Stripe** (test mode — needed from Stage 1.2 onward for the refund call; no real payments are ever made):
  1. Go to [dashboard.stripe.com](https://dashboard.stripe.com) and sign in (or create a free account) — no business details required to get test keys.
  2. Make sure the dashboard is in **Test mode** (toggle in the top-right corner).
  3. Go to [dashboard.stripe.com/test/apikeys](https://dashboard.stripe.com/test/apikeys) and copy the **Secret key** (starts with `sk_test_...`).
  4. Paste it into `.env` as `STRIPE_SECRET_KEY=sk_test_...`.

Never commit `.env` or paste real (non-test) keys into it — `.env` is already gitignored.

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
