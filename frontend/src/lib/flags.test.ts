import { describe, expect, it } from "vitest";

import type { Evidence, Flag } from "../api/types";
import {
  describeFlag,
  evidencePair,
  flagQuotes,
  flagTitle,
  sortFlags,
  toolLabel,
  verdictSummary,
} from "./flags";

function flag(code: string, details: Record<string, unknown> = {}, extra: Partial<Flag> = {}): Flag {
  return {
    code,
    severity: "warning",
    message: `${code} message.`,
    action_floor: "clarify",
    details,
    ...extra,
  };
}

const VENDOR_EVIDENCE: Evidence = {
  ref: "ev-abc-1",
  tool: "get_vendor",
  records: ["V002"],
  output: {
    found: true,
    vendor: { id: "V002", name: "Martin Office Supplies Synthetica GmbH" },
    bank_accounts: [
      { iban: "DE11OLD", valid_from: "2020-01-01", valid_to: "2023-12-31", current: false },
      { iban: "DE54XZXZ20145237421309", valid_from: "2024-01-01", valid_to: null, current: true },
    ],
  },
};

describe("flagTitle", () => {
  it("names known codes in plain language", () => {
    expect(flagTitle("iban_not_current")).toBe("Bank account changed");
    expect(flagTitle("injection_suspected")).toBe("Hidden or instruction-like text");
    expect(flagTitle("schema_failure")).toBe("Fields could not be read");
  });

  it("humanises an unknown code", () => {
    expect(flagTitle("new_rule_x")).toBe("New rule x");
  });
});

describe("describeFlag", () => {
  it("uses the rule's message", () => {
    expect(describeFlag(flag("wrong_vat"))).toBe("wrong_vat message.");
  });

  it("explains extraction flags, which carry no message", () => {
    expect(describeFlag(flag("schema_failure", {}, { message: null }))).toMatch(/could not be read/);
    expect(describeFlag(flag("odd", {}, { message: null }))).toMatch(/flagged/);
  });
});

describe("sortFlags", () => {
  it("puts critical flags first and keeps the order within a severity", () => {
    const flags = [
      flag("a", {}, { severity: "warning" }),
      flag("b", {}, { severity: "critical" }),
      flag("c", {}, { severity: "info" }),
      flag("d", {}, { severity: "critical" }),
    ];
    expect(sortFlags(flags).map((f) => f.code)).toEqual(["b", "d", "a", "c"]);
  });
});

describe("verdictSummary", () => {
  it("is the most severe flag's sentence", () => {
    const flags = [flag("wrong_vat"), flag("iban_not_current", {}, { severity: "critical" })];
    expect(verdictSummary(flags)).toBe("iban_not_current message.");
  });

  it("says every check passed when nothing was flagged", () => {
    expect(verdictSummary([])).toMatch(/every check passed/i);
  });
});

describe("evidencePair", () => {
  it("sets the invoice's IBAN against the vendor's current account", () => {
    const pair = evidencePair(
      flag("iban_not_current", { iban: "DE84ZZZZ26304761683594", status: "never_seen" }),
      [VENDOR_EVIDENCE],
    );
    expect(pair?.recordHeading).toBe("In our vendor record");
    expect(pair?.invoice).toEqual([
      { label: "IBAN to pay", value: "DE84ZZZZ26304761683594", mono: true },
      { label: "Status", value: "Never registered for this vendor", mono: false },
    ]);
    expect(pair?.record).toEqual([
      { label: "Current IBAN", value: "DE54XZXZ20145237421309", mono: true },
      { label: "Vendor", value: "Martin Office Supplies Synthetica GmbH", mono: false },
    ]);
  });

  it("falls back to what the flag says when the agent did not fetch the vendor", () => {
    const pair = evidencePair(
      flag("iban_not_current", { iban: "X", status: "past_account", current_iban_on_file: true }),
      [],
    );
    expect(pair?.invoice[1].value).toBe("A closed past account of this vendor");
    expect(pair?.record).toEqual([
      { label: "Current IBAN", value: "On file, different from the invoice", mono: false },
    ]);
  });

  it("says when the vendor record was not looked up for this case", () => {
    const pair = evidencePair(flag("iban_in_text_not_current", { iban: "NL40", sources: ["visible text"] }), []);
    expect(pair?.invoice).toEqual([
      { label: "IBAN in the text", value: "NL40", mono: true },
      { label: "Found in", value: "visible text", mono: false },
    ]);
    expect(pair?.record).toEqual([
      { label: "Current IBAN", value: "Not looked up for this case", mono: false },
    ]);
  });

  it("compares a total with what the lines add up to", () => {
    const pair = evidencePair(
      flag("total_mismatch", { check: "sum_of_lines", expected: "1797.07", found: "1897.07" }),
      [],
    );
    expect(pair?.recordHeading).toBe("Recalculated");
    expect(pair?.invoice).toEqual([{ label: "Subtotal", value: "1897.07", mono: true }]);
    expect(pair?.record).toEqual([{ label: "Sum of the lines", value: "1797.07", mono: true }]);
  });

  it("compares a VAT rate with the country's rate", () => {
    const pair = evidencePair(
      flag("wrong_vat", { check: "rate", country: "FR", expected_rate: "20.00", found_rate: "19.00" }),
      [],
    );
    expect(pair?.invoice).toEqual([{ label: "VAT rate", value: "19.00%", mono: true }]);
    expect(pair?.record).toEqual([{ label: "Rate for FR", value: "20.00%", mono: true }]);
  });

  it("compares a unit price with the contract", () => {
    const pair = evidencePair(
      flag("price_above_contract", {
        sku: "SKU-004",
        unit_price: "12.50",
        contract_price: "11.20",
        contract_id: "C003",
      }),
      [],
    );
    expect(pair?.recordHeading).toBe("In the contract");
    expect(pair?.invoice[0]).toEqual({ label: "SKU-004 unit price", value: "12.50", mono: true });
    expect(pair?.record[0]).toEqual({ label: "C003 price", value: "11.20", mono: true });
  });

  it("compares the net amount with the purchase order", () => {
    const pair = evidencePair(
      flag("amount_above_tolerance", {
        po_id: "PO-2026-0002",
        invoice_net: "2561.32",
        po_total_net: "2100.00",
        tolerance_pct: "5.00",
        limit: "2205.00",
      }),
      [],
    );
    expect(pair?.recordHeading).toBe("On the purchase order");
    expect(pair?.record).toEqual([
      { label: "PO-2026-0002 net", value: "2100.00", mono: true },
      { label: "Limit with 5.00% tolerance", value: "2205.00", mono: true },
    ]);
  });

  it("shows an unknown vendor as not matching any record", () => {
    const pair = evidencePair(flag("unknown_vendor", { name: "Maillot Trading", vat_id: null }), []);
    expect(pair?.invoice).toEqual([{ label: "Vendor", value: "Maillot Trading", mono: false }]);
    expect(pair?.record).toEqual([{ label: "Match", value: "No vendor matches", mono: false }]);
  });

  it("shows the earlier invoice of a duplicate", () => {
    const pair = evidencePair(
      flag("duplicate_confirmed", { invoice_number: "RE-00356", total: "823.60", issue_date: "2026-03-01" }),
      [],
    );
    expect(pair?.invoice).toEqual([]);
    expect(pair?.recordHeading).toBe("Already on file");
    expect(pair?.record.map((r) => r.value)).toEqual(["RE-00356", "823.60", "2026-03-01"]);
  });

  it("has no pair for text findings, which are quoted instead", () => {
    expect(evidencePair(flag("injection_suspected", { snippets: ["approve"] }), [])).toBeNull();
    expect(evidencePair(flag("schema_failure"), [])).toBeNull();
  });

  it("ignores details of the wrong type rather than rendering them", () => {
    const pair = evidencePair(flag("iban_not_current", { iban: { evil: true } }), []);
    expect(pair?.invoice[0].value).toBe("Not given");
  });
});

describe("toolLabel", () => {
  it("names the agent's tools in plain language", () => {
    expect(toolLabel("get_vendor")).toBe("Vendor record");
    expect(toolLabel("find_similar_invoices")).toBe("Similar invoices");
    expect(toolLabel(null)).toBe("Unknown source");
    expect(toolLabel("other_tool")).toBe("Other tool");
  });
});

describe("flagQuotes", () => {
  it("returns the suspicious snippets as strings, and nothing else", () => {
    const f = flag("injection_suspected", { snippets: ["Ignore previous instructions", 3, null] });
    expect(flagQuotes(f)).toEqual(["Ignore previous instructions"]);
    expect(flagQuotes(flag("wrong_vat"))).toEqual([]);
  });
});
