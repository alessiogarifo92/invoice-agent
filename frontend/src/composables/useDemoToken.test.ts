import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { KEY, createTokenStore } from "./useDemoToken";

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("createTokenStore", () => {
  it("keeps the token in sessionStorage only", () => {
    const store = createTokenStore();
    store.save("  tok-123  ");
    expect(store.token.value).toBe("tok-123");
    expect(sessionStorage.getItem(KEY)).toBe("tok-123");
    expect(localStorage.getItem(KEY)).toBeNull();
    expect(createTokenStore().token.value).toBe("tok-123");
  });

  it("forgets the token", () => {
    const store = createTokenStore();
    store.save("tok");
    store.forget();
    expect(store.token.value).toBe("");
    expect(sessionStorage.getItem(KEY)).toBeNull();
  });

  it("ignores a blank token", () => {
    const store = createTokenStore();
    store.save("   ");
    expect(store.token.value).toBe("");
    expect(sessionStorage.getItem(KEY)).toBeNull();
  });

  it("still works in memory when storage throws (private mode, blocked storage)", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "QuotaExceededError");
    });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    const store = createTokenStore();
    expect(store.token.value).toBe("");
    store.save("tok");
    expect(store.token.value).toBe("tok");
    store.forget();
    expect(store.token.value).toBe("");
  });

  it("marks a token the server rejected, until a new one is saved", () => {
    const store = createTokenStore();
    store.save("bad");
    store.reject();
    expect(store.rejected.value).toBe(true);
    expect(store.token.value).toBe("");
    expect(sessionStorage.getItem(KEY)).toBeNull();
    store.save("good");
    expect(store.rejected.value).toBe(false);
  });
});
