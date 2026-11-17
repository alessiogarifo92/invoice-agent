import { expect, type Page, type TestInfo } from "@playwright/test";

/** A full-page screenshot in e2e/screenshots, named after the project (desktop / phone). */
export async function shot(page: Page, testInfo: TestInfo, name: string, fullPage = true): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({
    path: `e2e/screenshots/${testInfo.project.name}-${name}.png`,
    fullPage,
    animations: "disabled",
  });
}

/** Every visible interactive target is at least 44 x 44 px (DESIGN.md, "Layout"). */
export async function expectTouchTargets(page: Page): Promise<void> {
  const small = await page.evaluate(() => {
    const selector = "a[href], button, .filter-option, input:not(.visually-hidden), textarea";
    return Array.from(document.querySelectorAll<HTMLElement>(selector))
      .filter((el) => el.offsetParent !== null && !el.closest("dialog:not([open])"))
      .filter((el) => el.getBoundingClientRect().top >= 0)
      .map((el) => {
        const box = el.getBoundingClientRect();
        return { what: el.outerHTML.slice(0, 80), w: Math.round(box.width), h: Math.round(box.height) };
      })
      .filter((t) => t.w < 44 || t.h < 44);
  });
  expect(small).toEqual([]);
}
