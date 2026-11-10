# Decisions

Each phase gate of the project lifecycle records its outcome here. Tickets reference these entries instead of re-deriving them.

## Phase 1 — Idea (confirmed 2026-11-10)

- **Direction:** invoice-agent, an AI agent for B2B supplier invoices (PDF). The reserve alternative (evolving `telegram-jobs-bot` into a job-matching agent) is not pursued.
- **Purpose:** portfolio project for AI / agents engineer roles in Amsterdam. Interview pitch: "I built an agent that handles money, and these are its defences against errors and fraud, measured with evals."
- **Flow:** extract structured data → RAG over purchase orders, contracts, vendor master data and past invoices → tool-based checks → anomaly flags → proposed action (approve / clarify / block) → human approval (no payment without it) → audit log of every step.
- **Anomalies covered:** amount vs PO beyond tolerance, changed IBAN (BEC fraud), duplicate invoice, unknown supplier, wrong VAT or total ≠ sum of lines, prices outside the contract, prompt injection inside the PDF.
- **Agent tools:** `get_purchase_order`, `get_vendor`, `find_similar_invoices`, `check_vat`, `propose_payment`.
- **Cross-cutting requirements:** RAG with citations, evals in CI (a regression fails the build), observability (traces, tokens, cost, latency), guardrails (Pydantic validation, tool limits, nothing irreversible without a human).
- **Out of scope:** real payments or bank integrations, multi-tenant auth, low-quality OCR.
- **Constraints:** synthetic data only; minimal cost (capped LLM budget, no fixed paid services); public repo, secrets only in CI/hosting.
- **Repo:** `alessiogarifo92/invoice-agent`, public.

## Phase 2 — Tools & cost

Confirmed by the user on 2026-11-10. Prices verified on the official pages on 2026-11-10 (Anthropic, Render, Neon, Supabase, Cloud Run, Cloudflare, Langfuse, Voyage); re-check at deploy (`deploy-readiness`).

Volume assumptions: 100-300 invoices/month today, ~3,000 at 10x; DB < 500 MB; cold start acceptable, data loss on inactivity not.

| Layer | Choice | Free tier / limit | Overrun / inactivity | Rejected |
|---|---|---|---|---|
| Backend (FastAPI, Docker) | Render Free web service | 750 instance-h/month, 512 MB RAM | spins down after 15 min, ~1 min cold start; hours exhausted = suspended until next month (hard stop) | Cloud Run (card required, no hard cap; kept as fallback), Fly.io (no free tier), Koyeb (unverified) |
| Postgres + pgvector | Neon Free | 100 CU-h/month per project, 0.5 GB storage | scales to zero after 5 min idle, data kept | Supabase Free (pauses after 7 days idle), Render Postgres Free (expires after 30 days) |
| Frontend (Vue 3 + Vite) | Cloudflare Pages / Workers static assets | 500 builds/month, 20k files, static requests free | — | Netlify, Vercel (not needed) |
| LLM tracing | Langfuse Cloud Hobby, via OpenTelemetry | 100k units/month, no card | — | Arize Phoenix (Elastic-2.0, not OSI) |
| Embeddings | Decided in phase 3: deterministic dedup first; if vectors are needed, local fastembed (Apache-2.0, $0) | — | — | Voyage voyage-3.5-lite ($0.02/MTok, no free tokens) |
| LLM | Haiku 5.5 extraction on every invoice; Sonnet 5.5 agent only on rule-flagged invoices (~15%); Opus 5.5 only in manual eval comparisons | dedicated Console workspace + key, spend limit $10/month | limit reached = API refuses until next month (no overage); app shows "demo budget exhausted", no blind retry | Opus as default agent (cost) |
| CI | GitHub Actions, public repo | standard runners unlimited | — | — |

Cost profile: fixed $0. LLM ~$4/month today (Haiku ~$0.001/invoice, Sonnet agent ~$0.08/flagged invoice), ~$40/month at 10x, never above the $10 cap without an explicit decision. Seed dataset processed via Batch API (-50%); prompt caching on the static system prompt.

Licenses: all dependencies MIT / Apache-2.0 / BSD; psycopg LGPL-3.0 used unmodified (OK). To verify in phase 3: Neon's pgvector extension availability and license.

Risks: Render + Neon cold start (loading page in the UI); Neon 0.5 GB (PDFs stay out of the DB); one awake Render service only, no keep-alive pinger; Haiku prompts < 100k tokens (price 5x above).

## Phase 3 — Architecture

_Not started._

## Phase 3.5 — Design UI/UX

_Not started._
