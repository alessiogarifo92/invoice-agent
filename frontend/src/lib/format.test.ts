import { describe, expect, it } from "vitest";

import { formatDateTime, formatMoney, paragraphs, shortHash } from "./format";

describe("formatMoney", () => {
  it("groups thousands without going through a float", () => {
    expect(formatMoney("1852.02", "EUR")).toBe("1,852.02 EUR");
    expect(formatMoney("123456789012345678.10", "EUR")).toBe("123,456,789,012,345,678.10 EUR");
    expect(formatMoney("-1200.5", null)).toBe("-1,200.5");
    expect(formatMoney("0.00", "EUR")).toBe("0.00 EUR");
  });

  it("shows a dash for a total not extracted yet, and leaves odd strings as they are", () => {
    expect(formatMoney(null, "EUR")).toBe("—");
    expect(formatMoney("12,5 EUR", "EUR")).toBe("12,5 EUR EUR");
  });
});

describe("formatDateTime", () => {
  it("is in UTC, so every viewer reads the same time", () => {
    expect(formatDateTime("2026-11-17T10:42:00Z")).toBe("17 Nov 2026, 10:42 UTC");
  });

  it("returns the input when it is not a date", () => {
    expect(formatDateTime("soon")).toBe("soon");
  });
});

describe("shortHash", () => {
  it("keeps the first 8 hex characters", () => {
    expect(shortHash("3f9ac21b00ffee")).toBe("3f9ac21b");
    expect(shortHash(null)).toBe("");
  });
});

describe("paragraphs", () => {
  it("splits prose on blank lines and drops empty ones", () => {
    expect(paragraphs("One.\n\n\nTwo\nlines.\n\n")).toEqual(["One.", "Two\nlines."]);
    expect(paragraphs("   ")).toEqual([]);
  });
});
