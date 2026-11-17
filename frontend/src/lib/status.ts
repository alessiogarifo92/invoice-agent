// Invoice status -> what the screens show. One table, so the inbox and the review screen
// can never describe the same status two ways.

import type { Action, Flag, InvoiceCard, InvoiceStatus } from "../api/types";

export const ACTION_LABEL: Record<Action, string> = {
  approve: "Approve",
  clarify: "Clarify",
  block: "Block",
};

export const ACTION_ORDER: Record<Action, number> = { approve: 0, clarify: 1, block: 2 };

export type CaseState = "processing" | "review" | "decided" | "quota" | "failed" | "unknown";

export interface StatusView {
  state: CaseState;
  label: string;
  // One or two words, for the pill of a case without a proposed action.
  short: string;
  summary: string;
  // A decided case: it sits on `tint` in the inbox (DESIGN.md, "Case card").
  closed: boolean;
}

const VIEWS: Record<InvoiceStatus, StatusView> = {
  received: {
    state: "processing",
    label: "Queued",
    short: "Queued",
    summary: "Waiting to be read.",
    closed: false,
  },
  processing: {
    state: "processing",
    label: "In progress",
    short: "In progress",
    summary: "Being read and checked against our records.",
    closed: false,
  },
  pending_approval: {
    state: "review",
    label: "Needs review",
    short: "Needs review",
    summary: "Waiting for a person to decide.",
    closed: false,
  },
  approved_for_payment: {
    state: "decided",
    label: "Approved",
    short: "Approved",
    summary: "Approved for payment by a reviewer. Nothing is paid by this demo.",
    closed: true,
  },
  clarification_requested: {
    state: "decided",
    label: "Clarification requested",
    short: "Clarifying",
    summary: "A reviewer asked the vendor to clarify.",
    closed: true,
  },
  blocked: {
    state: "decided",
    label: "Blocked",
    short: "Blocked",
    summary: "A reviewer blocked this payment.",
    closed: true,
  },
  awaiting_quota: {
    state: "quota",
    label: "Paused",
    short: "Paused",
    summary: "Paused: the demo budget is used up. It resumes after 00:00 UTC.",
    closed: false,
  },
  failed: {
    state: "failed",
    label: "Could not be processed",
    short: "Failed",
    summary: "Processing stopped after repeated errors. Nothing was decided.",
    closed: false,
  },
};

export function statusView(status: InvoiceStatus, finalAction: Action | null = null): StatusView {
  const view = VIEWS[status];
  if (!view) {
    return { state: "unknown", label: status, short: status, summary: "Status unknown.", closed: false };
  }
  if (status === "pending_approval" && finalAction) {
    return {
      ...view,
      summary: `Proposed: ${ACTION_LABEL[finalAction]}. Waiting for a person to decide.`,
    };
  }
  return { ...view };
}

// Polling stops on anything but the worker's own statuses. awaiting_quota waits for hours
// (until 00:00 UTC), so it stops too and offers a manual check instead.
export function isTerminal(status: InvoiceStatus): boolean {
  return status !== "received" && status !== "processing";
}

const STAGES: Record<string, string> = {
  extract: "Reading the invoice",
  rules: "Checking it against the purchase order, contract and vendor record",
  agent: "The agent is reviewing what the checks flagged",
  propose: "Setting the proposed action",
};

export function stageLabel(stage: string): string {
  return STAGES[stage] ?? "Working on it";
}

export type StatusFilter = "all" | "review" | "decided" | "in_progress" | "failed";
export type ActionFilter = "all" | Action;

export interface CaseFilter {
  status: StatusFilter;
  action: ActionFilter;
}

const STATUS_GROUPS: Record<Exclude<StatusFilter, "all">, InvoiceStatus[]> = {
  review: ["pending_approval"],
  decided: ["approved_for_payment", "clarification_requested", "blocked"],
  in_progress: ["received", "processing", "awaiting_quota"],
  failed: ["failed"],
};

export function matchesFilter(card: InvoiceCard, filter: CaseFilter): boolean {
  if (filter.status !== "all" && !STATUS_GROUPS[filter.status].includes(card.status)) return false;
  if (filter.action !== "all" && card.final_action !== filter.action) return false;
  return true;
}

export function extractionFailed(flags: Flag[]): boolean {
  return flags.some((f) => f.code === "schema_failure");
}
