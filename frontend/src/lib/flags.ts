// Flags in plain language (DESIGN.md, "Copy rules": plain language first, rule IDs second),
// and the evidence pair: what the invoice says next to what our records say.
// Every value comes from the flag's details or a tool result the agent cited; values of an
// unexpected type are shown as "Not given", never rendered as they are.

import type { Evidence, Flag } from "../api/types";

const TITLES: Record<string, string> = {
  iban_not_current: "Bank account changed",
  iban_in_text_not_current: "Another bank account in the document",
  unknown_vendor: "Unknown vendor",
  injection_suspected: "Hidden or instruction-like text",
  duplicate_confirmed: "Already invoiced: same invoice number",
  duplicate_suspected: "Possible duplicate",
  total_mismatch: "Totals do not add up",
  wrong_vat: "VAT is wrong",
  po_not_found: "Purchase order not found",
  po_vendor_mismatch: "Purchase order of another vendor",
  amount_above_tolerance: "Above the purchase order",
  po_balance_exceeded: "Purchase order already used up",
  currency_mismatch: "Different currency",
  price_above_contract: "Price above contract",
  conflicting_values_in_text: "Conflicting values in the document",
  missing_field: "A field could not be read",
  rule_error: "A check could not run",
  schema_failure: "Fields could not be read",
  grounding_failure: "Values not found in the text",
};

const DESCRIPTIONS: Record<string, string> = {
  schema_failure:
    "The invoice's fields could not be read into a valid record, so the checks ran on what was available and a person has to look.",
  grounding_failure: "Some extracted values do not appear in the invoice's own text.",
};

const TOOLS: Record<string, string> = {
  get_vendor: "Vendor record",
  get_purchase_order: "Purchase order",
  find_similar_invoices: "Similar invoices",
  check_vat: "VAT rates",
  search_contract_clauses: "Contract clauses",
};

const SEVERITY_ORDER: Record<string, number> = { critical: 0, warning: 1, info: 2 };

function humanise(code: string): string {
  const words = code.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function flagTitle(code: string): string {
  return TITLES[code] ?? humanise(code);
}

export function describeFlag(flag: Flag): string {
  return flag.message ?? DESCRIPTIONS[flag.code] ?? "The checks flagged this invoice for a person.";
}

export function sortFlags(flags: Flag[]): Flag[] {
  const rank = (f: Flag) => SEVERITY_ORDER[f.severity] ?? 3;
  return flags
    .map((flag, index) => ({ flag, index }))
    .sort((a, b) => rank(a.flag) - rank(b.flag) || a.index - b.index)
    .map(({ flag }) => flag);
}

export function verdictSummary(flags: Flag[]): string {
  const [first] = sortFlags(flags);
  if (!first) return "Every check passed against the purchase order, the contract and the vendor record.";
  return describeFlag(first);
}

export function toolLabel(tool: string | null): string {
  if (tool === null) return "Unknown source";
  return TOOLS[tool] ?? humanise(tool);
}

export function flagQuotes(flag: Flag): string[] {
  const snippets = flag.details.snippets;
  return Array.isArray(snippets) ? snippets.filter((s): s is string => typeof s === "string") : [];
}

export interface EvidenceRow {
  label: string;
  value: string;
  mono: boolean;
}

export interface EvidencePairView {
  invoice: EvidenceRow[];
  recordHeading: string;
  record: EvidenceRow[];
}

const NOT_GIVEN = "Not given";

function text(value: unknown): string | null {
  if (typeof value === "string" && value.trim() !== "") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

function row(label: string, value: unknown, mono = true): EvidenceRow {
  return { label, value: text(value) ?? NOT_GIVEN, mono };
}

function optionalRow(label: string, value: unknown, mono = true): EvidenceRow[] {
  return text(value) === null ? [] : [row(label, value, mono)];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const IBAN_STATUS: Record<string, string> = {
  never_seen: "Never registered for this vendor",
  past_account: "A closed past account of this vendor",
};

function vendorRecord(flag: Flag, evidence: Evidence[]): EvidenceRow[] {
  const output = evidence.find((e) => e.tool === "get_vendor" && isRecord(e.output))?.output;
  if (output && output.found === true) {
    const accounts = Array.isArray(output.bank_accounts) ? output.bank_accounts : [];
    const current = accounts
      .filter((a): a is Record<string, unknown> => isRecord(a) && a.current === true)
      .map((a) => row("Current IBAN", a.iban));
    const vendor = isRecord(output.vendor) ? output.vendor : {};
    return [
      ...(current.length ? current : [row("Current IBAN", "None on file", false)]),
      ...optionalRow("Vendor", vendor.name, false),
    ];
  }
  const onFile = flag.details.current_iban_on_file;
  const value =
    onFile === true
      ? "On file, different from the invoice"
      : onFile === false
        ? "None on file"
        : "Not looked up for this case";
  return [{ label: "Current IBAN", value, mono: false }];
}

function ibanStatusRow(status: unknown): EvidenceRow[] {
  const label = typeof status === "string" ? IBAN_STATUS[status] : undefined;
  return label ? [{ label: "Status", value: label, mono: false }] : [];
}

type PairBuilder = (d: Record<string, unknown>, flag: Flag, evidence: Evidence[]) => EvidencePairView | null;

const PAIRS: Record<string, PairBuilder> = {
  iban_not_current: (d, flag, evidence) => ({
    invoice: [row("IBAN to pay", d.iban), ...ibanStatusRow(d.status)],
    recordHeading: "In our vendor record",
    record: vendorRecord(flag, evidence),
  }),
  iban_in_text_not_current: (d, flag, evidence) => ({
    invoice: [
      row("IBAN in the text", d.iban),
      ...optionalRow(
        "Found in",
        Array.isArray(d.sources) ? d.sources.filter((s) => typeof s === "string").join(", ") : null,
        false,
      ),
    ],
    recordHeading: "In our vendor record",
    record: vendorRecord(flag, evidence),
  }),
  total_mismatch: (d) => {
    const labels: Record<string, [string, string]> = {
      line_total: [text(d.line_no) ? `Line ${d.line_no} total` : "Line total", "Quantity × unit price"],
      sum_of_lines: ["Subtotal", "Sum of the lines"],
      subtotal_plus_vat: ["Total", "Subtotal + VAT"],
    };
    const [found, expected] = labels[String(d.check)] ?? ["Amount", "Recalculated"];
    return {
      invoice: [row(found, d.found)],
      recordHeading: "Recalculated",
      record: [row(expected, d.expected)],
    };
  },
  wrong_vat: (d) =>
    d.check === "rate"
      ? {
          invoice: [row("VAT rate", text(d.found_rate) && `${d.found_rate}%`)],
          recordHeading: "Expected",
          record: [row(`Rate for ${text(d.country) ?? "the country"}`, text(d.expected_rate) && `${d.expected_rate}%`)],
        }
      : {
          invoice: [row("VAT amount", d.found)],
          recordHeading: "Recalculated",
          record: [row("VAT at the stated rate", d.expected)],
        },
  amount_above_tolerance: (d) => ({
    invoice: [row("Net amount", d.invoice_net)],
    recordHeading: "On the purchase order",
    record: [
      row(`${text(d.po_id) ?? "Purchase order"} net`, d.po_total_net),
      row(`Limit with ${text(d.tolerance_pct) ?? "the"}% tolerance`, d.limit),
    ],
  }),
  po_balance_exceeded: (d) => ({
    invoice: [row("Net amount", d.invoice_net), row("Billed with this invoice", d.cumulative)],
    recordHeading: "On the purchase order",
    record: [row("Already billed", d.billed_before), row("Limit", d.limit)],
  }),
  po_not_found: (d) => ({
    invoice: [row("Purchase order", d.po_number)],
    recordHeading: "In our purchase orders",
    record: [{ label: "Match", value: "No purchase order matches", mono: false }],
  }),
  po_vendor_mismatch: (d) => ({
    invoice: [row("Purchase order", d.po_id), row("Invoice vendor", d.invoice_vendor_id)],
    recordHeading: "In our purchase orders",
    record: [row("Belongs to vendor", d.po_vendor_id)],
  }),
  currency_mismatch: (d) => ({
    invoice: [row("Currency", d.invoice_currency)],
    recordHeading: d.against === "contract" ? "In the contract" : "On the purchase order",
    record: [row(text(d.reference) ?? "Expected", d.expected_currency)],
  }),
  price_above_contract: (d) => ({
    invoice: [row(`${text(d.sku) ?? "Item"} unit price`, d.unit_price)],
    recordHeading: "In the contract",
    record: [row(`${text(d.contract_id) ?? "Contract"} price`, d.contract_price)],
  }),
  conflicting_values_in_text: (d) => ({
    invoice: [row("Read as", d.extracted)],
    recordHeading: "Elsewhere in the document",
    record: [row("Also stated", d.found)],
  }),
  unknown_vendor: (d) => ({
    invoice: [
      ...optionalRow("Vendor", d.name, false),
      ...optionalRow("VAT id", d.vat_id),
      ...optionalRow("IBAN", d.iban),
    ],
    recordHeading: "In our vendor record",
    record: [{ label: "Match", value: "No vendor matches", mono: false }],
  }),
  duplicate_confirmed: (d) => duplicatePair(d),
  duplicate_suspected: (d) => duplicatePair(d),
};

function duplicatePair(d: Record<string, unknown>): EvidencePairView {
  return {
    invoice: [],
    recordHeading: "Already on file",
    record: [
      ...optionalRow("Invoice number", d.invoice_number),
      ...optionalRow("Total", d.total),
      ...optionalRow("Issue date", d.issue_date),
    ],
  };
}

export function evidencePair(flag: Flag, evidence: Evidence[]): EvidencePairView | null {
  const build = PAIRS[flag.code];
  return build ? build(isRecord(flag.details) ? flag.details : {}, flag, evidence) : null;
}
