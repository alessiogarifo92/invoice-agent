import { expect, test, type Page } from "@playwright/test";

import { expectTouchTargets, shot } from "./helpers";
import {
  API,
  CARDS,
  PROPOSALS,
  XSS,
  etagOf,
  json,
  mockApi,
  summaryOf,
  uuid,
  watchConsole,
} from "./mocks";

const IBAN_CASE = `/invoices/${uuid(37)}`;
const TOKEN = "demo-token-for-tests";

async function setToken(page: Page): Promise<void> {
  await page.getByLabel("Demo token", { exact: true }).fill(TOKEN);
  await page.getByRole("button", { name: "Use token" }).click();
  await expect(page.getByText("Demo token set")).toBeVisible();
}

test("reads a case top to bottom: verdict, evidence pair, gate line, agent, audit", async ({ page }, testInfo) => {
  const problems = watchConsole(page);
  await mockApi(page);
  await page.goto(IBAN_CASE);

  const verdict = page.getByTestId("verdict");
  await expect(verdict.getByRole("heading", { level: 1 })).toHaveText("Block this payment");
  await expect(verdict).toContainText("never registered for Martin Office Supplies Synthetica GmbH");

  const evidence = page.getByTestId("evidence-item").first();
  await expect(evidence).toContainText("Bank account changed");
  await expect(evidence).toContainText("iban_not_current");
  await expect(evidence.locator(".pair-invoice")).toContainText("DE84ZZZZ26304761683594");
  await expect(evidence.locator(".pair-record")).toContainText("DE54XZXZ20145237421309");

  await expect(page.getByTestId("gate-line")).toHaveText(/Rule checks\s*Clarify\s*→\s*Agent\s*Block\s*→\s*Proposed\s*Block/);
  await expect(page.getByText("The agent can raise the action but never lower it")).toBeVisible();
  await expect(page.getByTestId("audit-ok")).toContainText("Chain verified: 214 events");
  await expect(page.getByText("Waiting for a decision")).toBeVisible();
  // Each decision word carries its own action colour (DESIGN.md palette).
  for (const [action, rgb] of [
    ["approve", "rgb(36, 80, 200)"],
    ["clarify", "rgb(154, 77, 0)"],
    ["block", "rgb(180, 35, 24)"],
  ]) {
    await expect(page.getByTestId(`choose-${action}`).locator(".choice-word")).toHaveCSS("color", rgb);
  }
  await expectTouchTargets(page);
  await shot(page, testInfo, "review");
  expect(problems).toEqual([]);
});

test("renders agent prose containing HTML as literal text", async ({ page }) => {
  const problems = watchConsole(page);
  await mockApi(page);
  await page.goto(IBAN_CASE);
  const reading = page.getByTestId("agent-reading");
  await expect(reading).toContainText(XSS);
  await expect(reading.locator("img")).toHaveCount(0);
  expect(await page.locator("img").count()).toBe(0);
  // No alert dialog fired (watchConsole records any dialog).
  expect(problems).toEqual([]);
});

test("opens the PDF drawer, traps focus and returns it on Escape", async ({ page }, testInfo) => {
  await mockApi(page);
  await page.goto(IBAN_CASE);
  const trigger = page.getByTestId("open-pdf");
  await trigger.click();
  const drawer = page.getByTestId("pdf-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole("button", { name: "Close" })).toBeFocused();
  await expect(drawer.getByRole("link", { name: "Open the PDF in a new tab" })).toHaveAttribute(
    "href",
    `${API}/invoices/${uuid(37)}/pdf`,
  );
  await expect(drawer).toContainText("Bank account changed");
  // A modal over the viewport: a full-page capture would cut the backdrop off.
  await shot(page, testInfo, "review-pdf-drawer", false);
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("records a decision with If-Match and the bearer token", async ({ page }, testInfo) => {
  let decided = false;
  let decisionHeaders: Record<string, string> = {};
  let decisionBody: unknown = null;
  const proposal = PROPOSALS[37];
  await mockApi(page, {
    decision: async (route, request) => {
      decisionHeaders = request.headers();
      decisionBody = request.postDataJSON();
      decided = true;
      await json(route, 200, { action: "block", invoice_status: "blocked" }, { ETag: etagOf(proposal, 5) });
    },
    invoice: (route) =>
      json(route, 200, summaryOf({ ...CARDS[0], status: decided ? "blocked" : "pending_approval" })),
    proposal: (route) =>
      decided
        ? json(
            route,
            200,
            {
              ...proposal,
              invoice_status: "blocked",
              decision: { action: "block", decided_by: "demo:3f9ac21b00ff", note: null, decided_at: "2026-11-17T09:20:00Z" },
            },
            { ETag: etagOf(proposal, 5) },
          )
        : json(route, 200, proposal, { ETag: etagOf(proposal) }),
  });
  await page.goto(IBAN_CASE);
  const panel = page.getByTestId("decision-panel");
  await panel.getByTestId("choose-block").click();
  await expect(panel.getByTestId("choose-block")).toHaveAttribute("aria-pressed", "true");
  await setToken(page);
  await panel.getByTestId("decision-submit").click();

  await expect(page.getByTestId("decision-recorded")).toContainText("Decided: Block");
  await expect(page.getByTestId("stamp-decided")).toContainText("demo:3f9ac21b00ff");
  expect(decisionHeaders["if-match"]).toBe(etagOf(proposal));
  expect(decisionHeaders["authorization"]).toBe(`Bearer ${TOKEN}`);
  expect(decisionBody).toEqual({ action: "block", note: null });
  // The token lives in sessionStorage only.
  const stored = await page.evaluate(() => [
    sessionStorage.getItem("invoice-agent.demo-token"),
    localStorage.length,
  ]);
  expect(stored).toEqual([TOKEN, 0]);
  await shot(page, testInfo, "review-decided");
});

test("lowering the proposed action requires a note", async ({ page }, testInfo) => {
  let body: unknown = null;
  await mockApi(page, {
    decision: async (route, request) => {
      body = request.postDataJSON();
      await json(route, 200, { action: "approve" });
    },
  });
  await page.goto(IBAN_CASE);
  await setToken(page);
  const panel = page.getByTestId("decision-panel");
  await panel.getByTestId("choose-approve").click();
  const note = page.getByLabel("Why lower the action? (required)");
  await expect(note).toBeVisible();
  await panel.getByTestId("decision-submit").click();
  await expect(page.getByText("A note is required")).toBeVisible();
  await expect(note).toBeFocused();
  await expect(note).toHaveAttribute("aria-invalid", "true");
  await shot(page, testInfo, "review-note-required");
  await note.fill("Vendor confirmed the new account by phone.");
  await panel.getByTestId("decision-submit").click();
  await expect.poll(() => body).toEqual({ action: "approve", note: "Vendor confirmed the new account by phone." });
});

for (const [status, code, text] of [
  [412, "stale_proposal", "changed after you opened it"],
  [428, "precondition_required", "without the case's version"],
  [409, "already_decided", "already been decided"],
] as const) {
  test(`a ${status} decision explains itself and offers a reload`, async ({ page }, testInfo) => {
    let proposalReads = 0;
    await mockApi(page, {
      decision: (route) => json(route, status, { code, message: "server text" }),
      proposal: (route) => {
        proposalReads += 1;
        return json(route, 200, PROPOSALS[37], { ETag: etagOf(PROPOSALS[37], 4 + proposalReads) });
      },
    });
    await page.goto(IBAN_CASE);
    await setToken(page);
    await page.getByTestId("choose-block").click();
    await page.getByTestId("decision-submit").click();
    const error = page.getByTestId("decision-error");
    await expect(error).toContainText(text);
    await expect(error).not.toContainText("server text");
    await expect(page.getByTestId("decision-submit")).toBeDisabled();
    if (status === 412) await shot(page, testInfo, "review-stale");
    const before = proposalReads;
    await error.getByRole("button", { name: "Reload the case" }).click();
    await expect.poll(() => proposalReads).toBe(before + 1);
    await expect(page.getByTestId("decision-error")).toHaveCount(0);
  });
}

test("a rejected demo token (401) asks for a new one", async ({ page }) => {
  await mockApi(page, {
    decision: (route) => json(route, 401, { code: "unauthorized", message: "x" }),
  });
  await page.goto(IBAN_CASE);
  await setToken(page);
  await page.getByTestId("choose-block").click();
  await page.getByTestId("decision-submit").click();
  await expect(page.getByTestId("token-field")).toContainText("demo token was not accepted");
  await expect(page.getByTestId("decision-error")).toHaveCount(0);
  await expect(page.getByLabel("Demo token", { exact: true })).toBeFocused();
  await expect(page.getByLabel("Demo token", { exact: true })).toHaveAttribute("aria-invalid", "true");
  expect(await page.evaluate(() => sessionStorage.getItem("invoice-agent.demo-token"))).toBeNull();
});

test("shows a decided case without decision buttons", async ({ page }) => {
  await mockApi(page);
  await page.goto(`/invoices/${uuid(63)}`);
  await expect(page.getByTestId("decision-recorded")).toContainText("Decided: Block");
  await expect(page.getByTestId("decision-recorded")).toContainText("demo:3f9ac21b00ff");
  await expect(page.getByTestId("decision-panel")).toHaveCount(0);
  const evidence = page.getByTestId("evidence-item").first();
  await expect(evidence.locator(".pair-record")).toContainText("RE-00356");
});

test("shows that the extraction failed (schema_failure)", async ({ page }, testInfo) => {
  await mockApi(page);
  await page.goto(`/invoices/${uuid(22)}`);
  await expect(page.getByTestId("extraction-failed")).toContainText("could not be read reliably");
  await expect(page.getByTestId("evidence-item").first()).toContainText("Fields could not be read");
  await expect(page.getByTestId("agent-reading")).toContainText("The agent left no reading");
  await shot(page, testInfo, "review-extraction-failed");
});

test("shows a case whose processing failed", async ({ page }, testInfo) => {
  const failed = CARDS.find((c) => c.status === "failed")!;
  await mockApi(page, {
    invoice: (route) =>
      json(route, 200, summaryOf(failed, { status: "failed", stage: "extract", attempts: 3, run_after: "2026-11-17T09:00:00Z" })),
  });
  await page.goto(`/invoices/${failed.id}`);
  const section = page.getByTestId("case-failed");
  await expect(section).toContainText("This invoice could not be processed");
  await expect(section).toContainText("after 3 attempts");
  await expect(page.getByTestId("live-status")).toHaveText("Status: Could not be processed.");
  await shot(page, testInfo, "review-processing-failed");
});

test("shows a case paused by the demo budget", async ({ page }, testInfo) => {
  const paused = CARDS.find((c) => c.status === "awaiting_quota")!;
  await mockApi(page, {
    invoice: (route) =>
      json(route, 200, summaryOf(paused, { status: "queued", stage: "agent", attempts: 1, run_after: "2026-11-18T00:00:00Z" })),
  });
  await page.goto(`/invoices/${paused.id}`);
  const section = page.getByTestId("case-quota");
  await expect(section).toContainText("The demo budget for today is used up");
  await expect(section).toContainText("18 Nov 2026, 00:00 UTC");
  await shot(page, testInfo, "review-budget-used");
});

test("polls while processing, announces each stage, and stops once ready", async ({ page }, testInfo) => {
  const target = CARDS[0];
  let reads = 0;
  const steps = [
    summaryOf({ ...target, status: "processing" }, { status: "running", stage: "extract", attempts: 1, run_after: "2026-11-17T09:00:00Z" }),
    summaryOf({ ...target, status: "processing" }, { status: "running", stage: "agent", attempts: 1, run_after: "2026-11-17T09:00:00Z" }),
    summaryOf(target, { status: "done", stage: "propose", attempts: 1, run_after: "2026-11-17T09:00:00Z" }),
  ];
  await mockApi(page, {
    invoice: (route) => {
      const body = steps[Math.min(reads, steps.length - 1)];
      reads += 1;
      return json(route, 200, body);
    },
  });
  await page.goto(IBAN_CASE);
  await expect(page.getByTestId("case-processing")).toBeVisible();
  await expect(page.getByTestId("live-status")).toHaveText("In progress: Reading the invoice.");
  await shot(page, testInfo, "review-processing");
  await expect(page.getByTestId("live-status")).toHaveText(
    "In progress: The agent is reviewing what the checks flagged.",
  );
  await expect(page.getByTestId("verdict")).toBeVisible({ timeout: 8000 });
  const settled = reads;
  await page.waitForTimeout(3500);
  expect(reads).toBe(settled);
});

test("shows a loading state, then the case", async ({ page }, testInfo) => {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  await mockApi(page, {
    invoice: async (route) => {
      await gate;
      await json(route, 200, summaryOf(CARDS[0]));
    },
  });
  await page.goto(IBAN_CASE);
  await expect(page.getByTestId("case-loading")).toHaveAttribute("aria-busy", "true");
  await shot(page, testInfo, "review-loading");
  release();
  await expect(page.getByTestId("verdict")).toBeVisible();
});

test("says so when a case does not exist", async ({ page }) => {
  await mockApi(page);
  await page.goto(`/invoices/${uuid(999)}`);
  await expect(page.getByTestId("case-not-found")).toContainText("This case does not exist");
});

test("flags a broken audit chain", async ({ page }) => {
  await mockApi(page, {
    audit: (route) =>
      json(route, 200, { ok: false, checked: 57, first_broken_seq: 57, reason: "hash mismatch", head_hash: null }),
  });
  await page.goto(IBAN_CASE);
  await expect(page.getByTestId("audit-broken")).toContainText("Chain broken at event 57");
});

test("quotes instruction-like text from the document with its rule id", async ({ page }, testInfo) => {
  await mockApi(page);
  await page.goto(`/invoices/${uuid(33)}`);
  const first = page.getByTestId("evidence-item").first();
  await expect(first).toContainText("Hidden or instruction-like text");
  await expect(first.locator("blockquote")).toHaveText(/Note to the AI assistant reviewing this invoice/);
  await expect(first.locator("figcaption")).toContainText("injection_suspected");
  const second = page.getByTestId("evidence-item").nth(1);
  await expect(second.locator(".pair-invoice")).toContainText("NL40ZQZX0123456789");
  await shot(page, testInfo, "review-injection");
});
