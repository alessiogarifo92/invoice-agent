import { mount } from "@vue/test-utils";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App.vue";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App", () => {
  it("reports the API as reachable on a 200", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200 }));
    const wrapper = mount(App);
    await wrapper.find("button").trigger("click");
    await vi.waitFor(() => expect(wrapper.get("[data-testid=status]").text()).toBe("API reachable"));
  });

  it("shows a waking-up message when the API cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network")));
    const wrapper = mount(App);
    await wrapper.find("button").trigger("click");
    await vi.waitFor(() => expect(wrapper.get("[data-testid=status]").text()).toContain("unreachable"));
  });

  it("shows the HTTP status on a non-2xx answer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503 }));
    const wrapper = mount(App);
    await wrapper.find("button").trigger("click");
    await vi.waitFor(() => expect(wrapper.get("[data-testid=status]").text()).toBe("API error 503"));
  });
});
