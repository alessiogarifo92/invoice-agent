import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { effectScope } from "vue";

import { ApiError } from "../api/client";
import { useHealth } from "./useHealth";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

function setup(check: () => Promise<unknown>) {
  const scope = effectScope();
  const health = scope.run(() => useHealth(check, { slowMs: 2500, retryMs: 3000, maxBackoffMs: 15000 }))!;
  return { scope, health };
}

describe("useHealth", () => {
  it("is ok without a banner when /healthz answers quickly", async () => {
    const { health, scope } = setup(vi.fn().mockResolvedValue({ status: "ok" }));
    health.start();
    expect(health.state.value).toBe("checking");
    await vi.advanceTimersByTimeAsync(0);
    expect(health.state.value).toBe("ok");
    scope.stop();
  });

  it("shows waking up when /healthz is slow, then ok when it answers", async () => {
    let resolve: (v: unknown) => void = () => {};
    const check = vi.fn(() => new Promise((r) => (resolve = r)));
    const { health, scope } = setup(check);
    health.start();
    await vi.advanceTimersByTimeAsync(2499);
    expect(health.state.value).toBe("checking");
    await vi.advanceTimersByTimeAsync(1);
    expect(health.state.value).toBe("waking");
    resolve({ status: "ok" });
    await vi.advanceTimersByTimeAsync(0);
    expect(health.state.value).toBe("ok");
    scope.stop();
  });

  it("shows waking up on failure and retries with backoff until it answers", async () => {
    const check = vi
      .fn()
      .mockRejectedValueOnce(new ApiError("network", null, null))
      .mockRejectedValueOnce(new ApiError("server", 503, null))
      .mockResolvedValue({ status: "ok" });
    const { health, scope } = setup(check);
    health.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(health.state.value).toBe("waking");
    expect(health.attempts.value).toBe(1);
    await vi.advanceTimersByTimeAsync(6000);
    expect(check).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(12000);
    expect(check).toHaveBeenCalledTimes(3);
    expect(health.state.value).toBe("ok");
    // Once up, it stops checking.
    await vi.advanceTimersByTimeAsync(60000);
    expect(check).toHaveBeenCalledTimes(3);
    scope.stop();
  });

  it("retry() checks again at once", async () => {
    const check = vi.fn().mockRejectedValueOnce(new Error("x")).mockResolvedValue({ status: "ok" });
    const { health, scope } = setup(check);
    health.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(health.state.value).toBe("waking");
    health.retry();
    await vi.advanceTimersByTimeAsync(0);
    expect(check).toHaveBeenCalledTimes(2);
    expect(health.state.value).toBe("ok");
    scope.stop();
  });

  it("reports waking up when a data request found the server unreachable", async () => {
    const check = vi.fn().mockResolvedValue({ status: "ok" });
    const { health, scope } = setup(check);
    health.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(health.state.value).toBe("ok");
    health.reportUnreachable();
    expect(health.state.value).toBe("waking");
    await vi.advanceTimersByTimeAsync(0);
    expect(check).toHaveBeenCalledTimes(2);
    expect(health.state.value).toBe("ok");
    scope.stop();
  });
});
