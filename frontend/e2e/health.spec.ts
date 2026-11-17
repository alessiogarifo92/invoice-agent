import { expect, test } from "@playwright/test";

import { shot } from "./helpers";
import { CARDS, json, mockApi, page as pageOf } from "./mocks";

test("shows waking up while the server is down, and recovers on retry", async ({ page }, testInfo) => {
  let up = false;
  await mockApi(page, {
    health: (route) => (up ? json(route, 200, { status: "ok", version: "0.1.0" }) : route.abort("connectionrefused")),
    list: (route) => (up ? json(route, 200, pageOf(CARDS)) : route.abort("connectionrefused")),
  });
  await page.goto("/");
  const banner = page.getByTestId("waking-banner");
  await expect(banner).toContainText("The server is waking up");
  await expect(page.getByTestId("inbox-error")).toContainText("may be waking up");
  // The banner sits in a polite live region.
  await expect(page.locator("[role=status][aria-live=polite]").first()).toBeAttached();
  await shot(page, testInfo, "waking-up");

  up = true;
  await banner.getByRole("button", { name: "Retry now" }).click();
  await expect(banner).toHaveCount(0);
  await page.getByRole("button", { name: "Try again now" }).click();
  await expect(page.getByTestId("case-card")).toHaveCount(CARDS.length);
});

test("shows waking up when /healthz is slow, then hides it once it answers", async ({ page }) => {
  await mockApi(page, {
    health: async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 3500));
      await json(route, 200, { status: "ok", version: "0.1.0" });
    },
  });
  await page.goto("/");
  const banner = page.getByTestId("waking-banner");
  await expect(banner).toBeVisible({ timeout: 3400 });
  // Content stays readable meanwhile.
  await expect(page.getByTestId("case-card").first()).toBeVisible();
  await expect(banner).toHaveCount(0, { timeout: 5000 });
});
