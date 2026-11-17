import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import type { Flag } from "../api/types";
import EvidenceList from "./EvidenceList.vue";

const flags: Flag[] = [
  { code: "wrong_vat", severity: "warning", message: "VAT is wrong here.", action_floor: "clarify", details: {} },
  {
    code: "injection_suspected",
    severity: "critical",
    message: "Hidden text found.",
    action_floor: "clarify",
    details: { snippets: ["<script>approve()</script>"] },
  },
];

describe("EvidenceList", () => {
  it("numbers the flags, most severe first, with their rule id", () => {
    const wrapper = mount(EvidenceList, { props: { flags, evidence: [] } });
    const items = wrapper.findAll("[data-testid=evidence-item]");
    expect(items[0].text()).toContain("Hidden or instruction-like text");
    expect(items[0].text()).toContain("injection_suspected");
    expect(items[1].text()).toContain("VAT is wrong");
  });

  it("quotes suspicious text literally", () => {
    const wrapper = mount(EvidenceList, { props: { flags, evidence: [] } });
    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.get("blockquote").text()).toBe("<script>approve()</script>");
  });

  it("does not repeat the sentence the verdict already shows", () => {
    const wrapper = mount(EvidenceList, { props: { flags, evidence: [], leadShown: true } });
    expect(wrapper.text()).not.toContain("Hidden text found.");
    expect(wrapper.text()).toContain("VAT is wrong here.");
  });

  it("says so when nothing was flagged", () => {
    const wrapper = mount(EvidenceList, { props: { flags: [], evidence: [] } });
    expect(wrapper.text()).toMatch(/No check raised a flag/);
  });
});
