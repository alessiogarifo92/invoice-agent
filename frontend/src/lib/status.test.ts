import { describe, expect, it } from "vitest";

import type { InvoiceCard, InvoiceStatus } from "../api/types";
import {
  ACTION_LABEL,
  extractionFailed,
  isTerminal,
  matchesFilter,
  stageLabel,
  statusView,
} from "./status";

const ALL: InvoiceStatus[] = [
  "received",
  "processing",
  "pending_approval",
  "approved_for_payment",
  "clarification_requested",
  "blocked",
  "awaiting_quota",
  "failed",
];

function card(status: InvoiceStatus, final_action: InvoiceCard["final_action"] = null): InvoiceCard {
  return {
    id: "i",
    status,
    filename: "f.pdf",
    created_at: "2026-11-17T10:00:00Z",
    proposal_id: null,
    final_action,
    vendor_name: null,
    invoice_number: null,
    total: null,
    currency: null,
  };
}

describe("statusView", () => {
  it.each([
    ["received", "processing", false],
    ["processing", "processing", false],
    ["pending_approval", "review", true],
    ["approved_for_payment", "decided", true],
    ["clarification_requested", "decided", true],
    ["blocked", "decided", true],
    ["awaiting_quota", "quota", true],
    ["failed", "failed", true],
  ] as const)("maps %s to the %s state (terminal: %s)", (status, state, terminal) => {
    const view = statusView(status);
    expect(view.state).toBe(state);
    expect(isTerminal(status)).toBe(terminal);
    expect(view.label).not.toBe("");
    expect(view.short.length).toBeLessThanOrEqual(12);
    expect(view.summary).toMatch(/\.$/);
  });

  it("names the proposed action of a case waiting for review", () => {
    expect(statusView("pending_approval", "block").summary).toMatch(/Proposed: Block/);
  });

  it("says nothing is paid on approval and that quota pauses resume at 00:00 UTC", () => {
    expect(statusView("approved_for_payment").summary).toMatch(/nothing is paid/i);
    expect(statusView("awaiting_quota").summary).toMatch(/00:00 UTC/);
  });

  it("only the decided statuses are closed cases", () => {
    const closed = ALL.filter((s) => statusView(s).closed);
    expect(closed).toEqual(["approved_for_payment", "clarification_requested", "blocked"]);
  });

  it("falls back for a status this build does not know", () => {
    const view = statusView("archived" as InvoiceStatus);
    expect(view.state).toBe("unknown");
    expect(view.label).toBe("archived");
    expect(isTerminal("archived" as InvoiceStatus)).toBe(true);
  });
});

describe("stageLabel", () => {
  it.each([
    ["extract", /reading/i],
    ["rules", /checking/i],
    ["agent", /agent/i],
    ["propose", /proposed action/i],
  ])("describes the %s stage", (stage, pattern) => {
    expect(stageLabel(stage)).toMatch(pattern);
  });

  it("falls back to a generic sentence", () => {
    expect(stageLabel("later")).toMatch(/working/i);
  });
});

describe("ACTION_LABEL", () => {
  it("carries the word for every action, colour is never the only signal", () => {
    expect(ACTION_LABEL).toEqual({ approve: "Approve", clarify: "Clarify", block: "Block" });
  });
});

describe("matchesFilter", () => {
  it("passes everything with no filter", () => {
    expect(ALL.every((s) => matchesFilter(card(s), { status: "all", action: "all" }))).toBe(true);
  });

  it.each([
    ["review", ["pending_approval"]],
    ["decided", ["approved_for_payment", "clarification_requested", "blocked"]],
    ["in_progress", ["received", "processing", "awaiting_quota"]],
    ["failed", ["failed"]],
  ] as const)("the %s status filter keeps %j", (status, kept) => {
    const result = ALL.filter((s) => matchesFilter(card(s), { status, action: "all" }));
    expect(result).toEqual(kept);
  });

  it("filters on the proposed action, and a case without one never matches", () => {
    expect(matchesFilter(card("pending_approval", "block"), { status: "all", action: "block" })).toBe(
      true,
    );
    expect(
      matchesFilter(card("pending_approval", "clarify"), { status: "all", action: "block" }),
    ).toBe(false);
    expect(matchesFilter(card("processing"), { status: "all", action: "approve" })).toBe(false);
  });
});

describe("extractionFailed", () => {
  it("is true when the extraction raised schema_failure", () => {
    const flag = { severity: "warning", message: null, action_floor: "clarify", details: {} };
    expect(extractionFailed([{ code: "schema_failure", ...flag }])).toBe(true);
    expect(extractionFailed([{ code: "wrong_vat", ...flag }])).toBe(false);
    expect(extractionFailed([])).toBe(false);
  });
});
