import { describe, expect, it, vi } from "vitest";

import { ApiError, createApi, errorFromResponse, userMessage } from "./client";

const BASE = "http://api.test";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function fakeFetch(...responses: Array<Response | Error>) {
  const fn = vi.fn<typeof fetch>();
  for (const r of responses) {
    if (r instanceof Error) fn.mockRejectedValueOnce(r);
    else fn.mockResolvedValueOnce(r);
  }
  return fn;
}

function requestOf(fn: ReturnType<typeof fakeFetch>, call = 0): { url: string; init: RequestInit } {
  const [url, init] = fn.mock.calls[call];
  return { url: String(url), init: init ?? {} };
}

function header(init: RequestInit, name: string): string | null {
  return new Headers(init.headers).get(name);
}

describe("errorFromResponse", () => {
  it.each([
    [401, "unauthorized", "unauthorized"],
    [428, "precondition_required", "precondition_required"],
    [412, "stale_proposal", "stale"],
    [409, "already_decided", "conflict"],
    [409, "proposal_superseded", "conflict"],
    [422, "note_required", "unprocessable"],
    [404, "not_found", "not_found"],
    [413, "too_large", "too_large"],
    [415, "not_pdf", "unsupported"],
    [500, "boom", "server"],
    [503, "unavailable", "server"],
  ])("maps %i %s to the %s kind", (status, code, kind) => {
    const error = errorFromResponse(status, { code, message: "server text" });
    expect(error).toBeInstanceOf(ApiError);
    expect(error.kind).toBe(kind);
    expect(error.status).toBe(status);
    expect(error.code).toBe(code);
  });

  it("keeps the kind when the body is not the API's error shape", () => {
    const error = errorFromResponse(412, "<html>proxy</html>");
    expect(error.kind).toBe("stale");
    expect(error.code).toBeNull();
  });

  it("treats a FastAPI validation body (detail list) as unprocessable", () => {
    const error = errorFromResponse(422, { detail: [{ loc: ["body", "action"] }] });
    expect(error.kind).toBe("unprocessable");
    expect(error.code).toBeNull();
  });
});

describe("userMessage", () => {
  it("explains a rejected demo token", () => {
    expect(userMessage(errorFromResponse(401, { code: "unauthorized", message: "x" }))).toMatch(
      /demo token was not accepted/i,
    );
  });

  it("explains a stale case and a missing version", () => {
    expect(userMessage(errorFromResponse(412, { code: "stale_proposal", message: "x" }))).toMatch(
      /changed after you opened it/i,
    );
    expect(
      userMessage(errorFromResponse(428, { code: "precondition_required", message: "x" })),
    ).toMatch(/reload/i);
  });

  it("tells already-decided from superseded conflicts", () => {
    expect(userMessage(errorFromResponse(409, { code: "already_decided", message: "x" }))).toMatch(
      /already been decided/i,
    );
    expect(
      userMessage(errorFromResponse(409, { code: "proposal_superseded", message: "x" })),
    ).toMatch(/newer proposal/i);
    expect(
      userMessage(errorFromResponse(409, { code: "invalid_transition", message: "x" })),
    ).toMatch(/not waiting for a decision/i);
  });

  it("asks for a note when lowering the action", () => {
    expect(userMessage(errorFromResponse(422, { code: "note_required", message: "x" }))).toMatch(
      /note/i,
    );
  });

  it("names the upload limits in plain language", () => {
    expect(userMessage(errorFromResponse(413, { code: "too_large", message: "x" }))).toMatch(/2 MB/);
    expect(userMessage(errorFromResponse(415, { code: "not_pdf", message: "x" }))).toMatch(/PDF/);
    expect(userMessage(errorFromResponse(422, { code: "page_limit", message: "x" }))).toMatch(
      /5 pages/,
    );
    expect(userMessage(errorFromResponse(422, { code: "no_text_layer", message: "x" }))).toMatch(
      /text layer/i,
    );
  });

  it("never echoes the server's own message text", () => {
    const message = userMessage(errorFromResponse(500, { code: "boom", message: "<b>leak</b>" }));
    expect(message).not.toContain("leak");
  });

  it("describes network failures as the server waking up", () => {
    expect(userMessage(new ApiError("network", null, null))).toMatch(/waking up/i);
  });
});

describe("createApi", () => {
  it("lists invoices with paging parameters", async () => {
    const page = { items: [], total: 0, limit: 100, offset: 0 };
    const fetchImpl = fakeFetch(jsonResponse(200, page));
    const api = createApi(BASE, fetchImpl);
    await expect(api.listInvoices(100, 0)).resolves.toEqual(page);
    expect(requestOf(fetchImpl).url).toBe(`${BASE}/invoices?limit=100&offset=0`);
  });

  it("drops a trailing slash from the base URL", async () => {
    const fetchImpl = fakeFetch(jsonResponse(200, { status: "ok", version: "1" }));
    await createApi(`${BASE}/`, fetchImpl).health();
    expect(requestOf(fetchImpl).url).toBe(`${BASE}/healthz`);
  });

  it("returns the proposal with its ETag", async () => {
    const fetchImpl = fakeFetch(jsonResponse(200, { id: "p1" }, { ETag: '"p1.3"' }));
    const { proposal, etag } = await createApi(BASE, fetchImpl).getProposal("p1");
    expect(proposal).toEqual({ id: "p1" });
    expect(etag).toBe('"p1.3"');
  });

  it("refuses a proposal without an ETag (CORS must expose it)", async () => {
    const fetchImpl = fakeFetch(jsonResponse(200, { id: "p1" }));
    await expect(createApi(BASE, fetchImpl).getProposal("p1")).rejects.toMatchObject({
      kind: "bad_response",
    });
  });

  it("sends a decision with If-Match and the bearer token", async () => {
    const fetchImpl = fakeFetch(jsonResponse(200, { action: "block" }, { ETag: '"p1.4"' }));
    const api = createApi(BASE, fetchImpl);
    const { result, etag } = await api.decide("p1", { action: "block", note: null }, '"p1.3"', "tok");
    expect(result).toEqual({ action: "block" });
    expect(etag).toBe('"p1.4"');
    const { url, init } = requestOf(fetchImpl);
    expect(url).toBe(`${BASE}/proposals/p1/decision`);
    expect(init.method).toBe("POST");
    expect(header(init, "If-Match")).toBe('"p1.3"');
    expect(header(init, "Authorization")).toBe("Bearer tok");
    expect(header(init, "Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({ action: "block", note: null });
  });

  it.each([
    [401, "unauthorized", "unauthorized"],
    [412, "stale_proposal", "stale"],
    [428, "precondition_required", "precondition_required"],
    [409, "already_decided", "conflict"],
    [422, "note_required", "unprocessable"],
  ])("rejects a %i decision as %s", async (status, code, kind) => {
    const fetchImpl = fakeFetch(jsonResponse(status, { code, message: "m" }));
    await expect(
      createApi(BASE, fetchImpl).decide("p1", { action: "approve", note: null }, '"e"', "tok"),
    ).rejects.toMatchObject({ kind, status, code });
  });

  it("uploads the file as multipart with the bearer token", async () => {
    const accepted = { id: "i1", status: "received", sha256: "a".repeat(64) };
    const fetchImpl = fakeFetch(jsonResponse(202, accepted));
    const file = new File(["%PDF-1.4"], "inv.pdf", { type: "application/pdf" });
    const { invoice, created } = await createApi(BASE, fetchImpl).upload(file, "tok");
    expect(invoice).toEqual(accepted);
    expect(created).toBe(true);
    const { url, init } = requestOf(fetchImpl);
    expect(url).toBe(`${BASE}/invoices`);
    expect(header(init, "Authorization")).toBe("Bearer tok");
    // The browser sets the multipart boundary itself.
    expect(header(init, "Content-Type")).toBeNull();
    const body = init.body as FormData;
    expect((body.get("file") as File).name).toBe("inv.pdf");
  });

  it("reports an upload of bytes already stored (200) as not created", async () => {
    const fetchImpl = fakeFetch(jsonResponse(200, { id: "i1", status: "blocked", sha256: "b" }));
    const file = new File(["%PDF"], "inv.pdf", { type: "application/pdf" });
    const { created } = await createApi(BASE, fetchImpl).upload(file, "tok");
    expect(created).toBe(false);
  });

  it("maps a rejected fetch to a network error", async () => {
    const fetchImpl = fakeFetch(new TypeError("Failed to fetch"));
    await expect(createApi(BASE, fetchImpl).getInvoice("i1")).rejects.toMatchObject({
      kind: "network",
      status: null,
    });
  });

  it("maps a request that outlives its timeout to a timeout error", async () => {
    vi.useFakeTimers();
    try {
      const fetchImpl = vi.fn<typeof fetch>(
        (_url, init) =>
          new Promise((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () =>
              reject(new DOMException("aborted", "AbortError")),
            );
          }),
      );
      const pending = createApi(BASE, fetchImpl).health(1000);
      const assertion = expect(pending).rejects.toMatchObject({ kind: "timeout" });
      await vi.advanceTimersByTimeAsync(1000);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it("maps a 200 that is not JSON to a bad response", async () => {
    const fetchImpl = fakeFetch(new Response("<html>", { status: 200 }));
    await expect(createApi(BASE, fetchImpl).verifyAudit()).rejects.toMatchObject({
      kind: "bad_response",
    });
  });

  it("builds the PDF URL with an encoded id", () => {
    expect(createApi(BASE, fakeFetch()).pdfUrl("a/b")).toBe(`${BASE}/invoices/a%2Fb/pdf`);
  });
});
