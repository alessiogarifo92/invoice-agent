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

_Pending user confirmation._

## Phase 3 — Architecture

_Not started._

## Phase 3.5 — Design UI/UX

_Not started._
