import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { effectScope } from "vue";

import { nextDelay, usePolling } from "./usePolling";

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

beforeEach(() => {
  vi.useFakeTimers();
  setHidden(false);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("nextDelay", () => {
  it("is the interval while nothing failed", () => {
    expect(nextDelay(2500, 30000, 0)).toBe(2500);
  });

  it("doubles per consecutive failure, up to the cap", () => {
    expect(nextDelay(2500, 30000, 1)).toBe(5000);
    expect(nextDelay(2500, 30000, 2)).toBe(10000);
    expect(nextDelay(2500, 30000, 3)).toBe(20000);
    expect(nextDelay(2500, 30000, 4)).toBe(30000);
    expect(nextDelay(2500, 30000, 40)).toBe(30000);
  });
});

describe("usePolling", () => {
  function setup<T>(task: () => Promise<T>, isDone?: (value: T) => boolean) {
    const scope = effectScope();
    const polling = scope.run(() =>
      usePolling(task, { intervalMs: 2500, maxBackoffMs: 30000, isDone }),
    )!;
    return { scope, polling };
  }

  it("runs at once, then every interval", async () => {
    const task = vi.fn().mockResolvedValue("x");
    const { polling, scope } = setup(task);
    polling.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(1);
    expect(polling.data.value).toBe("x");
    await vi.advanceTimersByTimeAsync(2500);
    expect(task).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2500);
    expect(task).toHaveBeenCalledTimes(3);
    scope.stop();
  });

  it("never overlaps two requests: the next one waits for the slow one", async () => {
    let resolve: (value: string) => void = () => {};
    const task = vi.fn(() => new Promise<string>((r) => (resolve = r)));
    const { polling, scope } = setup(task);
    polling.start();
    await vi.advanceTimersByTimeAsync(10000);
    expect(task).toHaveBeenCalledTimes(1);
    resolve("late");
    await vi.advanceTimersByTimeAsync(2500);
    expect(task).toHaveBeenCalledTimes(2);
    scope.stop();
  });

  it("stops once the value is terminal", async () => {
    const task = vi
      .fn()
      .mockResolvedValueOnce("processing")
      .mockResolvedValueOnce("pending_approval");
    const { polling, scope } = setup(task, (v: string) => v !== "processing");
    polling.start();
    await vi.advanceTimersByTimeAsync(2500);
    expect(task).toHaveBeenCalledTimes(2);
    expect(polling.done.value).toBe(true);
    expect(polling.active.value).toBe(false);
    await vi.advanceTimersByTimeAsync(60000);
    expect(task).toHaveBeenCalledTimes(2);
    scope.stop();
  });

  it("backs off on errors and resets after a success", async () => {
    const task = vi
      .fn()
      .mockRejectedValueOnce(new Error("1"))
      .mockRejectedValueOnce(new Error("2"))
      .mockResolvedValue("ok");
    const { polling, scope } = setup(task);
    polling.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(polling.failures.value).toBe(1);
    expect(polling.error.value).toBeInstanceOf(Error);
    // First retry after 5 s, not 2.5 s.
    await vi.advanceTimersByTimeAsync(4999);
    expect(task).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(task).toHaveBeenCalledTimes(2);
    expect(polling.failures.value).toBe(2);
    // Second retry after 10 s.
    await vi.advanceTimersByTimeAsync(9999);
    expect(task).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(task).toHaveBeenCalledTimes(3);
    expect(polling.failures.value).toBe(0);
    expect(polling.error.value).toBeNull();
    // Back to the plain interval.
    await vi.advanceTimersByTimeAsync(2500);
    expect(task).toHaveBeenCalledTimes(4);
    scope.stop();
  });

  it("keeps the last good value while a later poll fails", async () => {
    const task = vi.fn().mockResolvedValueOnce("good").mockRejectedValueOnce(new Error("x"));
    const { polling, scope } = setup(task);
    polling.start();
    await vi.advanceTimersByTimeAsync(2500);
    expect(polling.data.value).toBe("good");
    expect(polling.failures.value).toBe(1);
    scope.stop();
  });

  it("pauses while the tab is hidden and polls at once when it is visible again", async () => {
    const task = vi.fn().mockResolvedValue("x");
    const { polling, scope } = setup(task);
    polling.start();
    await vi.advanceTimersByTimeAsync(0);
    setHidden(true);
    await vi.advanceTimersByTimeAsync(60000);
    expect(task).toHaveBeenCalledTimes(1);
    setHidden(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2500);
    expect(task).toHaveBeenCalledTimes(3);
    scope.stop();
  });

  it("does not start polling while the tab is hidden", async () => {
    setHidden(true);
    const task = vi.fn().mockResolvedValue("x");
    const { polling, scope } = setup(task);
    polling.start();
    await vi.advanceTimersByTimeAsync(10000);
    expect(task).not.toHaveBeenCalled();
    setHidden(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(task).toHaveBeenCalledTimes(1);
    scope.stop();
  });

  it("does not resume on visibility after it finished", async () => {
    const task = vi.fn().mockResolvedValue("done");
    const { polling, scope } = setup(task, () => true);
    polling.start();
    await vi.advanceTimersByTimeAsync(0);
    setHidden(true);
    setHidden(false);
    await vi.advanceTimersByTimeAsync(10000);
    expect(task).toHaveBeenCalledTimes(1);
    scope.stop();
  });

  it("stop() cancels the next poll and drops an answer still in flight", async () => {
    let resolve: (value: string) => void = () => {};
    const task = vi.fn(() => new Promise<string>((r) => (resolve = r)));
    const { polling, scope } = setup(task);
    polling.start();
    await vi.advanceTimersByTimeAsync(0);
    polling.stop();
    resolve("late");
    await vi.advanceTimersByTimeAsync(10000);
    expect(polling.data.value).toBeNull();
    expect(task).toHaveBeenCalledTimes(1);
    scope.stop();
  });

  it("refresh() polls at once and restarts a finished poll", async () => {
    const task = vi.fn().mockResolvedValueOnce("a").mockResolvedValueOnce("b").mockResolvedValue("c");
    const { polling, scope } = setup(task, (v: string) => v === "a" || v === "c");
    polling.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(polling.done.value).toBe(true);
    polling.refresh();
    await vi.advanceTimersByTimeAsync(0);
    expect(polling.data.value).toBe("b");
    expect(polling.active.value).toBe(true);
    await vi.advanceTimersByTimeAsync(2500);
    expect(polling.data.value).toBe("c");
    expect(polling.done.value).toBe(true);
    scope.stop();
  });

  it("stops and removes its listener when its scope is disposed", async () => {
    const task = vi.fn().mockResolvedValue("x");
    const { polling, scope } = setup(task);
    polling.start();
    await vi.advanceTimersByTimeAsync(0);
    scope.stop();
    setHidden(true);
    setHidden(false);
    await vi.advanceTimersByTimeAsync(10000);
    expect(task).toHaveBeenCalledTimes(1);
  });
});
