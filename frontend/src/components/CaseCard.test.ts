import { RouterLinkStub, mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import type { InvoiceCard } from "../api/types";
import CaseCard from "./CaseCard.vue";

function card(fields: Partial<InvoiceCard>): InvoiceCard {
  return {
    id: "i1",
    status: "pending_approval",
    filename: "INV-SEED-037.pdf",
    created_at: "2026-11-17T09:00:00Z",
    proposal_id: "p1",
    final_action: "block",
    vendor_name: "Martin Office Supplies Synthetica GmbH",
    invoice_number: "2026/326",
    total: "1852.02",
    currency: "EUR",
    ...fields,
  };
}

function render(c: InvoiceCard) {
  return mount(CaseCard, { props: { card: c }, global: { stubs: { RouterLink: RouterLinkStub } } });
}

describe("CaseCard", () => {
  it("shows vendor, printed number, action word, total and links to the case", () => {
    const wrapper = render(card({}));
    expect(wrapper.get(".case-title").text()).toBe("Martin Office Supplies Synthetica GmbH");
    expect(wrapper.get(".case-ref").text()).toBe("2026/326");
    expect(wrapper.get(".pill").text()).toBe("Block");
    expect(wrapper.get(".case-total").text()).toBe("1,852.02 EUR");
    expect(wrapper.getComponent(RouterLinkStub).props("to")).toEqual({
      name: "review",
      params: { id: "i1" },
    });
  });

  it("uses the filename as the title, once, before anything was extracted", () => {
    const wrapper = render(
      card({ status: "failed", final_action: null, vendor_name: null, invoice_number: null, total: null }),
    );
    expect(wrapper.get(".case-title").text()).toBe("INV-SEED-037.pdf");
    expect(wrapper.find(".case-ref").exists()).toBe(false);
    expect(wrapper.get(".pill").text()).toBe("Failed");
    expect(wrapper.get(".case-total").text()).toBe("—");
  });

  it("puts a decided case on the tint background", () => {
    expect(render(card({ status: "blocked" })).get("a").classes()).toContain("closed");
    expect(render(card({})).get("a").classes()).not.toContain("closed");
  });

  it("renders vendor-controlled names as text", () => {
    const wrapper = render(card({ vendor_name: "<img src=x onerror=alert(1)>" }));
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.get(".case-title").text()).toBe("<img src=x onerror=alert(1)>");
  });
});
