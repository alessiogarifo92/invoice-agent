import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import PlainProse from "./PlainProse.vue";

describe("PlainProse", () => {
  it("renders HTML-looking agent prose as literal text", () => {
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    const text = 'Pay now. <img src=x onerror=alert(1)>\n\n<script>alert(2)</script><b>bold</b>';
    const wrapper = mount(PlainProse, { props: { text } });
    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.find("script").exists()).toBe(false);
    expect(wrapper.find("b").exists()).toBe(false);
    expect(wrapper.text()).toContain("<img src=x onerror=alert(1)>");
    expect(wrapper.text()).toContain("<script>alert(2)</script><b>bold</b>");
    expect(wrapper.html()).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(alert).not.toHaveBeenCalled();
  });

  it("keeps one paragraph per blank-line block", () => {
    const wrapper = mount(PlainProse, { props: { text: "First.\n\nSecond." } });
    expect(wrapper.findAll("p").map((p) => p.text())).toEqual(["First.", "Second."]);
  });

  it("shows a placeholder for empty prose", () => {
    const wrapper = mount(PlainProse, { props: { text: "  ", empty: "No reading." } });
    expect(wrapper.text()).toBe("No reading.");
  });
});
