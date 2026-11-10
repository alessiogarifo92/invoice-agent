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

Risks: Render + Neon cold start (loading page in the UI); Neon 0.5 GB (PDFs stay out of the DB — superseded in phase 3); one awake Render service only, no keep-alive pinger; Haiku prompts < 100k tokens (price 5x above).

## Phase 3 — Architecture

Confirmed by the user on 2026-11-10, after one `architect` proposal and one `critic` review (cross-verification Mode A). The user accepted every recommendation below.

**Components.** One FastAPI process on Render, in modules that depend only on the ones below them:
- `api`: thin routers.
- `ingestion`: pdfplumber (MIT; PyMuPDF rejected, AGPL). Extracts text plus each character's colour and size, so hidden text can be detected.
- `extraction`: Haiku, Pydantic schema, grounding check.
- `matching`: plain SQL lookups.
- `rules`: pure functions, no DB and no LLM.
- `agent`: the Sonnet loop and its policy gate.
- `review`: the human-approval state machine.
- `audit`: append-only hash chain.
- `llm`: the single gateway for every Claude call (budget guard, error classifier, cost ledger, OTel).
- `jobs`: the Postgres queue and its worker.

The Vue app is static on Cloudflare Pages and calls the API through a CORS allowlist. Neon holds all state.

**Flow.**
1. Upload: PDF only, ≤ 2 MB, ≤ 5 pages, a text layer is required. A sha256 makes it idempotent. One transaction writes the invoice, the job and an audit event, then the API returns 202.
2. Worker (A2, see below): extract → grounding → matching → rules → Sonnet agent, only when a rule raised a flag → policy gate → proposal → `pending_approval`.
3. The UI polls every 2-3 s and shows a "waking up" state during a cold start.
4. A human decides via `POST /proposals/{id}/decision` with `If-Match`. "Approve" only sets `approved_for_payment`; nothing is ever paid.

**Background processing: A2.**
- A Postgres `jobs` table, claimed with `FOR UPDATE SKIP LOCKED` by a worker inside the web process.
- Each stage stores its output, so a restart resumes from the last completed stage and never pays for it twice.
- A partial unique index stops the same invoice being enqueued twice.
- Jobs carry a lease with a heartbeat. Only jobs whose lease has expired are requeued, which keeps an old instance's job safe during an overlapping deploy.
- pdfplumber and other CPU-bound work runs in a thread or process pool, never on the event loop.
- `/healthz` does not touch the DB. The startup sweep runs lazily. Connections use `pool_pre_ping`.
- Rejected: A1 `BackgroundTasks` (work silently lost on spin-down); A3 synchronous requests (browser timeouts, no recovery).

**Retrieval: B1, no vectors in v1.**
- Duplicates: exact match on (vendor, normalised number), then fuzzy on amount ± a date window plus trigram on the number.
- Vendor: exact VAT id or IBAN, otherwise trigram on the name.
- Contract clauses: Postgres full-text search.
- Citations are record IDs, checked in code (API citations are incompatible with structured outputs).
- Trigger for B2 (pgvector + embeddings): a Layer-3 eval showing a clause-recall gap. Options then: fastembed (local, RAM against 512 MB unverified), or Voyage pay-per-use (cents/month, which the constraint allows because it is not a fixed fee).

**Data model (sketch).**
- Master data:
  - `vendors`: unique VAT id, trigram index on the name.
  - `vendor_bank_accounts`: IBAN history with validity dates.
  - `purchase_orders` and `po_lines`.
  - `contracts`, `contract_prices` and `contract_clauses` (with a tsvector).
- Processing:
  - `invoices`: unique file hash; the PDF bytes are stored in the DB (see the decision below).
  - `processing_runs`: model and prompt versions.
  - `extractions`: indexed for duplicate search, deliberately not unique.
  - `flags`, `proposals`, `approvals`.
  - `llm_calls`: the source of truth for spend.
  - `jobs`, `budget_state`.
- `audit_events` hash chain:
  - Each insert takes `pg_advisory_xact_lock`, so concurrent writers cannot fork the chain; a test runs concurrent writers.
  - The app role has INSERT/SELECT only, plus a trigger that rejects UPDATE and DELETE.
  - The app role is created with SQL `CREATE ROLE`, never from the Neon console: console-created roles get `neon_superuser`, whose `pg_write_all_data` bypasses the REVOKE.
  - Migrations use a separate owner connection secret.
  - A verify endpoint walks the chain. The chain is tamper-evident only, since the owner role could recompute it. The head hash is published externally (Langfuse or a commit).

**Rules (deterministic, fail closed).**
- Checks: totals vs the sum of lines, VAT rate by country, amount vs PO within tolerance, the PO belongs to this vendor, cumulative billing vs the remaining PO balance, currency mismatch, price vs contract per SKU, IBAN not the vendor's current one, unknown vendor, duplicate candidates, hidden or instruction-like text.
- Rules check **every** IBAN, invoice number and total that a regex finds in the PDF text, hidden text included, not only the values Haiku extracted.
- A missing or unparsable field, or an exception inside a rule, raises a flag. It never counts as a pass.

**Agent.**
- Tools: `get_purchase_order`, `get_vendor`, `find_similar_invoices`, `check_vat`, `search_contract_clauses` (added to the phase 1 list), and the terminal `propose_payment`.
- Every tool is strict, read-only and bound to the invoice under review.
- `tool_choice: auto`, because forced tool choice returns a 400 on Sonnet 5.5. Ending without calling `propose_payment` counts as `clarify`.
- Limits: at most 8 tool calls, a task budget and a timeout; reaching any of them counts as `clarify`.
- Thinking: `between_tools` or low effort; Sonnet 5.5 cannot disable thinking. Thinking is disabled on Haiku.
- Role: **triage and explanation only.** It judges ambiguous evidence (resend vs credit note, a partial delivery), writes the rationale and drafts the clarification message.
- Policy gate: `final_action = max(rule_floor, agent_action)`. The agent can raise severity, never lower it:
  - an IBAN change or an injection is at least `clarify`;
  - a confirmed duplicate is `block`;
  - a refusal or a schema failure is `clarify`.
- An `evidence_ref` that this run's tools did not return is rejected, and the proposal becomes `clarify`.
- Agent prose is attacker-influenced (it comes from untrusted PDFs and earlier extractions). The UI renders it as plain text, shows rule evidence first, and never sends the clarification message automatically.

**Prompt-injection defence.**
- Invoice text goes into a delimited block labelled as untrusted data. Operator instructions stay in `system`.
- The extraction schema has no free-text field that the agent would read as an instruction.
- Hidden text and instruction patterns raise a deterministic flag.
- No tool can pay or approve.

**Spend control.**
- SDK `max_retries=0`, with our own classifier.
- Retried: 429s that carry `retry-after`, 5xx and 529.
- Never retried: the workspace-limit 400 (narrow match on "You have reached your specified ... API usage limits") and the tier-cap 429 `enforced_spend_limit_reached`.
- Either limit error sets the invoice to `awaiting_budget` and `budget_state.exhausted_until`. That date is taken from the message, falling back to the 1st of next month UTC. The UI shows a "demo budget exhausted" banner.
- The app ledger stops at a soft cap of $9 before Anthropic's $10 hard stop. There is also a daily sub-cap per demo token.

**Public exposure (decided).**
- The public demo runs live only on a gallery of seeded synthetic invoices.
- Upload and approve sit behind a demo token, with a daily spend sub-cap, so real invoices and budget burning are kept out.

**PDF storage (decided; supersedes "PDFs stay out of the DB" in phase 2).**
- PDFs (~40 KB each) are stored in the DB.
- Render's disk is not persistent, and the PDFs can be regenerated from the seed anyway.
- Neon storage is to be re-checked: the critic reads the current Free plan as 1 GB, phase 2 recorded 0.5 GB.

**CI budget (decided).**
- A second Console workspace and key for CI, with its own limit (e.g. $5/month). The user creates it in the Console.
- LLM jobs skip PRs from forks.

**Evals.**
- Layer 1, every PR, $0: rules and matching on ground-truth extractions. Recall and precision per anomaly; approving any fraud or injection case is a hard fail.
- Layer 2, every PR, ~$0.03: Haiku extraction on ~25 invoices, compared with `evals/baseline.json`.
- Layer 3, ~$1.2 per run: the full pipeline with Sonnet on ~15 flagged cases. Run only on demand or via a PR label, never nightly.

**Observability.**
- FastAPI OTel instrumentation, the Anthropic SDK's own spans, and our own span per stage.
- `BatchSpanProcessor` exports over OTLP/HTTP to `cloud.langfuse.com/api/public/otel`.
- `force_flush` runs at the end of each job.

**Synthetic data.**
- A `synth/` package with a fixed seed, using Faker, Jinja2 and WeasyPrint (ReportLab as fallback).
- WeasyPrint runs only in the generator or in CI, never in the API image.
- A ground-truth JSON is written next to each PDF.
- Mutators: IBAN swap, resend with a reformatted number, amount above tolerance, unknown vendor, wrong VAT, total ≠ sum of lines, price above contract, injection (visible note, white-on-white text, tiny font, metadata).
- Every PDF carries a "SYNTHETIC" watermark. IBANs pass mod-97 but use made-up bank codes.

**Ticket order.**
0. Skeleton and CI, plus a **walking-skeleton deploy**: `/healthz` on Render, a migration on Neon, the restricted role, and one traced call.
1. Generator.
2. Schema, audit chain, roles.
3. Ingestion.
4. Rules and matching, plus the Layer-1 eval.
5. LLM gateway.
6. Haiku extraction, plus the Layer-2 eval.
7. Jobs and worker.
8. Sonnet agent, plus the Layer-3 eval.
9. HITL approval and demo-token auth.
10. Frontend (blocked by phase 3.5).
11. Full deploy (`deploy-readiness`).

**To verify (non-blocking).**
- Render: spin-down and SIGTERM grace period, behaviour during overlapping deploys, CPU share.
- Memory under 512 MB.
- Neon storage quota.
- Prompt-cache minimum on the 5.5 models.
- The Langfuse OTLP headers.
- The Sonnet cost per flagged invoice.
- Whether batch jobs stop at the workspace limit.

## Phase 3.5 — Design UI/UX

**Decided 2026-11-10: direction C "Case File".** Full tokens in [`DESIGN.md`](../DESIGN.md).

- Compared on one canvas (private, owner-only): https://claude.ai/artifact/KzzVogMqmcH6MgwDzg9pFP. Three directions, each drawn on the same two screens (Inbox, Invoice review):
  - A "Ledger": dense table, footnote markers on the PDF.
  - B "Control Room": dark console, lanes per action, three-pane review.
  - C "Case File": white, narrative single column, one case per invoice explained in a sentence.
- Why C: the reviewer and a portfolio visitor read each case top to bottom: verdict, evidence, how the action was set (rule floor vs agent), agent rationale, decision, audit chain. The anomaly is understandable without decoding a table or a dashboard.
- Kept from the other directions: numbered evidence markers on the PDF (A). They link the PDF drawer to the evidence section.
- UI copy never names models; the "server waking up" and "demo budget used" states are first-class banners, not errors.
