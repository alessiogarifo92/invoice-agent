// Response shapes of the FastAPI backend (backend/app/api/*.py). Dates are ISO strings,
// amounts decimal strings as printed on the invoice.

export type Action = "approve" | "clarify" | "block";

export type InvoiceStatus =
  | "received"
  | "processing"
  | "pending_approval"
  | "approved_for_payment"
  | "clarification_requested"
  | "blocked"
  | "awaiting_quota"
  | "failed";

export interface InvoiceCard {
  id: string;
  status: InvoiceStatus;
  filename: string;
  created_at: string;
  proposal_id: string | null;
  final_action: Action | null;
  vendor_name: string | null;
  invoice_number: string | null;
  total: string | null;
  currency: string | null;
}

export interface InvoicePage {
  items: InvoiceCard[];
  total: number;
  limit: number;
  offset: number;
}

export interface JobState {
  status: "queued" | "running" | "done" | "failed";
  stage: string;
  attempts: number;
  run_after: string;
}

export interface InvoiceSummary {
  id: string;
  status: InvoiceStatus;
  sha256: string;
  filename: string;
  page_count: number;
  created_at: string;
  job: JobState | null;
  proposal_id: string | null;
}

export interface Flag {
  code: string;
  severity: string;
  message: string | null;
  action_floor: string | null;
  details: Record<string, unknown>;
}

export interface Evidence {
  ref: string;
  tool: string | null;
  records: string[];
  output: Record<string, unknown> | null;
}

export interface Decision {
  action: Action;
  decided_by: string;
  note: string | null;
  decided_at: string;
}

export interface Proposal {
  id: string;
  invoice_id: string;
  invoice_status: InvoiceStatus;
  rule_floor: Action;
  agent_action: Action | null;
  final_action: Action;
  // Attacker-influenced prose: plain text only, never HTML.
  rationale: string;
  created_at: string;
  is_current: boolean;
  flags: Flag[];
  evidence: Evidence[];
  decision: Decision | null;
}

export interface DecisionResult {
  proposal_id: string;
  invoice_id: string;
  action: Action;
  proposed_action: Action;
  override: boolean;
  lowered: boolean;
  invoice_status: InvoiceStatus;
  decided_by: string;
  note: string | null;
  decided_at: string;
}

export interface InvoiceAccepted {
  id: string;
  status: InvoiceStatus;
  sha256: string;
}

export interface AuditVerify {
  ok: boolean;
  checked: number;
  first_broken_seq: number | null;
  reason: string | null;
  head_hash: string | null;
}

export interface Health {
  status: string;
  version: string;
}
