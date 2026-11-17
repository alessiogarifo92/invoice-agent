// The one place that talks to the backend. Every failure becomes an ApiError with a kind
// the screens switch on; userMessage() turns it into our own plain-language sentence, so a
// server's (or a proxy's) text is never shown.

import type {
  Action,
  AuditVerify,
  DecisionResult,
  Health,
  InvoiceAccepted,
  InvoicePage,
  InvoiceSummary,
  Proposal,
} from "./types";

export type ApiErrorKind =
  | "network"
  | "timeout"
  | "unauthorized"
  | "precondition_required"
  | "stale"
  | "conflict"
  | "unprocessable"
  | "not_found"
  | "too_large"
  | "unsupported"
  | "server"
  | "bad_response";

export class ApiError extends Error {
  constructor(
    readonly kind: ApiErrorKind,
    readonly status: number | null,
    readonly code: string | null,
  ) {
    super(`${kind}${status === null ? "" : ` (${status})`}${code ? `: ${code}` : ""}`);
    this.name = "ApiError";
  }
}

const KIND_BY_STATUS: Record<number, ApiErrorKind> = {
  401: "unauthorized",
  404: "not_found",
  409: "conflict",
  412: "stale",
  413: "too_large",
  415: "unsupported",
  422: "unprocessable",
  428: "precondition_required",
};

function errorCode(body: unknown): string | null {
  if (typeof body === "object" && body !== null && "code" in body) {
    const code = (body as { code: unknown }).code;
    return typeof code === "string" ? code : null;
  }
  return null;
}

export function errorFromResponse(status: number, body: unknown): ApiError {
  const kind = KIND_BY_STATUS[status] ?? (status >= 500 ? "server" : "bad_response");
  return new ApiError(kind, status, errorCode(body));
}

const MESSAGE_BY_CODE: Record<string, string> = {
  already_decided: "This case has already been decided. Reload it to see the decision.",
  proposal_superseded: "A newer proposal replaced this one. Reload the case to decide on it.",
  invalid_transition: "This case is not waiting for a decision any more. Reload it.",
  note_required: "Lowering the proposed action needs a note saying why.",
  unknown_action: "Choose Approve, Clarify or Block.",
  too_large: "The file is larger than 2 MB.",
  not_pdf: "Only PDF files can be uploaded.",
  unreadable_pdf: "The PDF is corrupt or cannot be read.",
  page_limit: "The PDF has more than 5 pages.",
  no_text_layer: "The PDF has no text layer (a scan or an image). Upload a PDF with selectable text.",
};

const MESSAGE_BY_KIND: Record<ApiErrorKind, string> = {
  network: "The server is not answering. It may be waking up; this takes about 30 seconds.",
  timeout: "The server is taking too long to answer. It may be waking up.",
  unauthorized: "The demo token was not accepted. Check it and try again.",
  precondition_required:
    "The decision was sent without the case's version. Reload the case and decide again.",
  stale:
    "This case changed after you opened it. Reload it to see the latest version, then decide again.",
  conflict: "This case cannot be decided in its current state. Reload it.",
  unprocessable: "The request was not accepted. Check the form and try again.",
  not_found: "This case does not exist.",
  too_large: "The file is larger than 2 MB.",
  unsupported: "Only PDF files can be uploaded.",
  server: "The server ran into a problem. Try again in a moment.",
  bad_response: "The server sent an answer this page cannot read. Try again in a moment.",
};

export function userMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return MESSAGE_BY_KIND.bad_response;
  return (error.code && MESSAGE_BY_CODE[error.code]) || MESSAGE_BY_KIND[error.kind];
}

export function isUnreachable(error: unknown): boolean {
  return error instanceof ApiError && (error.kind === "network" || error.kind === "timeout");
}

export interface DecisionInput {
  action: Action;
  note: string | null;
}

export interface Api {
  health(timeoutMs?: number): Promise<Health>;
  listInvoices(limit: number, offset: number): Promise<InvoicePage>;
  getInvoice(id: string): Promise<InvoiceSummary>;
  getProposal(id: string): Promise<{ proposal: Proposal; etag: string }>;
  decide(
    id: string,
    input: DecisionInput,
    etag: string,
    token: string,
  ): Promise<{ result: DecisionResult; etag: string | null }>;
  upload(file: File, token: string): Promise<{ invoice: InvoiceAccepted; created: boolean }>;
  verifyAudit(): Promise<AuditVerify>;
  pdfUrl(id: string): string;
}

const DEFAULT_TIMEOUT_MS = 20000;

export function createApi(
  baseUrl: string,
  fetchImpl: typeof fetch = (...args) => fetch(...args),
): Api {
  const base = baseUrl.replace(/\/+$/, "");

  async function request(
    path: string,
    init: RequestInit = {},
    timeoutMs = DEFAULT_TIMEOUT_MS,
  ): Promise<{ body: unknown; response: Response }> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response: Response;
    try {
      response = await fetchImpl(`${base}${path}`, { ...init, signal: controller.signal });
    } catch {
      throw new ApiError(controller.signal.aborted ? "timeout" : "network", null, null);
    } finally {
      clearTimeout(timer);
    }
    let body: unknown = null;
    let parsed = true;
    try {
      body = await response.json();
    } catch {
      parsed = false;
    }
    if (!response.ok) throw errorFromResponse(response.status, body);
    if (!parsed) throw new ApiError("bad_response", response.status, null);
    return { body, response };
  }

  async function get<T>(path: string, timeoutMs?: number): Promise<T> {
    return (await request(path, {}, timeoutMs)).body as T;
  }

  const id = (value: string) => encodeURIComponent(value);

  return {
    health: (timeoutMs = 8000) => get<Health>("/healthz", timeoutMs),
    listInvoices: (limit, offset) =>
      get<InvoicePage>(`/invoices?limit=${limit}&offset=${offset}`),
    getInvoice: (invoiceId) => get<InvoiceSummary>(`/invoices/${id(invoiceId)}`),
    async getProposal(proposalId) {
      const { body, response } = await request(`/proposals/${id(proposalId)}`);
      const etag = response.headers.get("ETag");
      // Without it no decision can be sent; a missing CORS expose header shows up here.
      if (!etag) throw new ApiError("bad_response", response.status, "missing_etag");
      return { proposal: body as Proposal, etag };
    },
    async decide(proposalId, input, etag, token) {
      const { body, response } = await request(`/proposals/${id(proposalId)}/decision`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
          "If-Match": etag,
        },
        body: JSON.stringify({ action: input.action, note: input.note }),
      });
      return { result: body as DecisionResult, etag: response.headers.get("ETag") };
    },
    async upload(file, token) {
      const form = new FormData();
      form.append("file", file, file.name);
      const { body, response } = await request("/invoices", {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: form,
      });
      return { invoice: body as InvoiceAccepted, created: response.status === 202 };
    },
    verifyAudit: () => get<AuditVerify>("/audit/verify"),
    pdfUrl: (invoiceId) => `${base}/invoices/${id(invoiceId)}/pdf`,
  };
}
