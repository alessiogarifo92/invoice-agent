import { expect, test } from "@playwright/test";

import { expectTouchTargets, shot } from "./helpers";
import { CARDS, SEED_PDF, json, mockApi, summaryOf, uuid } from "./mocks";

test("asks for the token, and explains a rejected one (401)", async ({ page }, testInfo) => {
  let auth: string | undefined;
  await mockApi(page, {
    upload: (route, request) => {
      auth = request.headers()["authorization"];
      return json(route, 401, { code: "unauthorized", message: "x" });
    },
  });
  await page.goto("/upload");
  await page.getByTestId("upload-file").setInputFiles(SEED_PDF);
  await page.getByTestId("upload-submit").click();
  await expect(page.getByTestId("upload-error")).toContainText("Enter the demo token first.");
  await expect(page.getByLabel("Demo token", { exact: true })).toBeFocused();

  await page.getByLabel("Demo token", { exact: true }).fill("wrong-token");
  await page.getByRole("button", { name: "Use token" }).click();
  await page.getByTestId("upload-submit").click();
  await expect(page.getByTestId("token-field")).toContainText("demo token was not accepted");
  await expect(page.getByTestId("upload-error")).toHaveCount(0);
  await expect(page.getByLabel("Demo token", { exact: true })).toHaveAttribute("aria-invalid", "true");
  expect(auth).toBe("Bearer wrong-token");
  await expectTouchTargets(page);
  await shot(page, testInfo, "upload-unauthorized");
});

test("uploads a PDF and opens its case, which updates as it is processed", async ({ page }, testInfo) => {
  const target = { ...CARDS[0], id: uuid(80), filename: "INV-SEED-037.pdf" };
  let contentType = "";
  let reads = 0;
  await mockApi(page, {
    upload: (route, request) => {
      contentType = request.headers()["content-type"] ?? "";
      return json(route, 202, { id: target.id, status: "received", sha256: "b".repeat(64) });
    },
    invoice: (route) => {
      reads += 1;
      return json(
        route,
        200,
        summaryOf({ ...target, status: "received", proposal_id: null }, {
          status: "queued",
          stage: "extract",
          attempts: 0,
          run_after: "2026-11-17T09:00:00Z",
        }),
      );
    },
  });
  await page.goto("/upload");
  await page.getByLabel("Demo token", { exact: true }).fill("good-token");
  await page.getByRole("button", { name: "Use token" }).click();
  await page.getByTestId("upload-file").setInputFiles(SEED_PDF);
  await expect(page.getByText("INV-SEED-037.pdf")).toBeVisible();
  await shot(page, testInfo, "upload-ready");
  await page.getByTestId("upload-submit").click();
  await expect(page).toHaveURL(new RegExp(`/invoices/${target.id}\\?uploaded=new`));
  await expect(page.getByTestId("uploaded-note")).toContainText("Uploaded.");
  await expect(page.getByTestId("case-processing")).toContainText("Queued");
  expect(contentType).toMatch(/^multipart\/form-data; boundary=/);
  await expect.poll(() => reads).toBeGreaterThan(1);
});

test("refuses a file over 2 MB before sending it", async ({ page }) => {
  const requests = await mockApi(page);
  await page.goto("/upload");
  await page.getByTestId("upload-file").setInputFiles({
    name: "big.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.alloc(2_100_000, 32),
  });
  await expect(page.getByText("The file is larger than 2 MB.")).toBeVisible();
  await page.getByTestId("upload-submit").click();
  expect(requests.filter((r) => r.method() === "POST")).toHaveLength(0);
});
