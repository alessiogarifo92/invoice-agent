import { expect, test } from "@playwright/test";

import { expectTouchTargets, shot } from "./helpers";
import { CARDS, json, mockApi, page as pageOf, watchConsole } from "./mocks";

test("lists every case with its action, status and the synthetic-data notice", async ({ page }, testInfo) => {
  const problems = watchConsole(page);
  await mockApi(page);
  await page.goto("/");
  const cards = page.getByTestId("case-card");
  await expect(cards).toHaveCount(CARDS.length);
  const first = cards.first();
  await expect(first).toContainText("Block");
  await expect(first).toContainText("Martin Office Supplies Synthetica GmbH");
  await expect(first).toContainText("1,852.02 EUR");
  await expect(first).toContainText("Proposed: Block. Waiting for a person to decide.");
  // A decided case sits on the tint background.
  await expect(cards.filter({ hasText: "RE 356" })).toHaveClass(/closed/);
  await expect(cards.filter({ hasText: "march-supplies.pdf" })).toContainText("In progress");
  await expect(cards.filter({ hasText: "scan-0042.pdf" })).toContainText("Could not be processed");
  // One case is paused by the quota: the budget banner is up.
  await expect(page.getByTestId("quota-banner")).toContainText("demo budget for today is used up");
  await expect(page.getByText("Synthetic data", { exact: true })).toBeVisible();
  await expect(page.getByTestId("case-count")).toHaveText(String(CARDS.length));
  await expectTouchTargets(page);
  await shot(page, testInfo, "inbox");
  expect(problems).toEqual([]);
});

test("filters by proposed action and by status, and keeps the filter in the URL", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  const cards = page.getByTestId("case-card");
  await expect(cards).toHaveCount(CARDS.length);

  await page.locator("label.filter-option", { hasText: "Block" }).click();
  await expect(cards).toHaveCount(2);
  await expect(page).toHaveURL(/action=block/);

  await page.locator("label.filter-option", { hasText: "Decided" }).click();
  await expect(cards).toHaveCount(1);
  await expect(cards.first()).toContainText("RE 356");

  await page.locator("label.filter-option", { hasText: "Failed" }).click();
  await expect(page.getByTestId("inbox-no-match")).toBeVisible();
  await page.getByRole("button", { name: "Clear the filters" }).click();
  await expect(cards).toHaveCount(CARDS.length);
  await expect(page).toHaveURL(/\/$/);

  // A link with a filter opens filtered.
  await page.goto("/?status=review");
  await expect(cards).toHaveCount(3);
});

test("shows the empty inbox", async ({ page }, testInfo) => {
  await mockApi(page, { list: (route) => json(route, 200, pageOf([])) });
  await page.goto("/");
  await expect(page.getByTestId("inbox-empty")).toContainText("No cases yet");
  await expect(page.getByRole("link", { name: "Submit an invoice" }).last()).toBeVisible();
  await shot(page, testInfo, "inbox-empty");
});

test("shows a loading state until the cases arrive", async ({ page }, testInfo) => {
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => (release = resolve));
  await mockApi(page, {
    list: async (route) => {
      await gate;
      await json(route, 200, pageOf(CARDS));
    },
  });
  await page.goto("/");
  const loading = page.getByTestId("inbox-loading");
  await expect(loading).toBeVisible();
  await expect(loading).toHaveAttribute("aria-busy", "true");
  await shot(page, testInfo, "inbox-loading");
  release();
  await expect(page.getByTestId("case-card")).toHaveCount(CARDS.length);
});
