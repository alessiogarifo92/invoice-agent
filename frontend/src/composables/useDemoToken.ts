// The demo token (T9: `Authorization: Bearer <token>`), kept for this tab only. Storage can
// throw (private mode, blocked third-party storage), so every access is guarded and the
// token then simply lives in memory until the tab closes.

import { ref, type Ref } from "vue";

export const KEY = "invoice-agent.demo-token";

function read(): string {
  try {
    return window.sessionStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

function write(value: string | null): void {
  try {
    if (value === null) window.sessionStorage.removeItem(KEY);
    else window.sessionStorage.setItem(KEY, value);
  } catch {
    // In memory only.
  }
}

export interface TokenStore {
  token: Readonly<Ref<string>>;
  // The server answered 401 to the last token: the screens ask for a new one.
  rejected: Readonly<Ref<boolean>>;
  save(value: string): void;
  forget(): void;
  reject(): void;
}

export function createTokenStore(): TokenStore {
  const token = ref(read());
  const rejected = ref(false);
  return {
    token,
    rejected,
    save(value) {
      const trimmed = value.trim();
      if (!trimmed) return;
      token.value = trimmed;
      rejected.value = false;
      write(trimmed);
    },
    forget() {
      token.value = "";
      write(null);
    },
    reject() {
      token.value = "";
      rejected.value = true;
      write(null);
    },
  };
}

let shared: TokenStore | null = null;

// One store for the whole app: the upload screen and the decision panel share the token.
export function useDemoToken(): TokenStore {
  shared ??= createTokenStore();
  return shared;
}
