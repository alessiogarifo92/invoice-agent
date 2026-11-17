// Server liveness for the "waking up" banner (DESIGN.md, "States"). Render's free tier sleeps
// when idle and takes ~30 s to start: /healthz answering slowly, or failing, means waking up.
// It keeps checking with backoff until the server answers, then stops.

import { getCurrentScope, onScopeDispose, ref, watch, type Ref } from "vue";

import { usePolling } from "./usePolling";

export type HealthState = "checking" | "ok" | "waking";

export interface HealthOptions {
  slowMs?: number;
  retryMs?: number;
  maxBackoffMs?: number;
}

export interface HealthMonitor {
  state: Readonly<Ref<HealthState>>;
  attempts: Readonly<Ref<number>>;
  start(): void;
  retry(): void;
  // A data request found the server unreachable: show the banner and check again.
  reportUnreachable(): void;
}

export function useHealth(check: () => Promise<unknown>, options: HealthOptions = {}): HealthMonitor {
  const slowMs = options.slowMs ?? 2500;
  const state = ref<HealthState>("checking");
  const attempts = ref(0);
  let slowTimer: ReturnType<typeof setTimeout> | null = null;

  const polling = usePolling(
    async () => {
      attempts.value += 1;
      await check();
      return true;
    },
    {
      intervalMs: options.retryMs ?? 3000,
      maxBackoffMs: options.maxBackoffMs ?? 15000,
      isDone: () => true,
    },
  );

  function clearSlow(): void {
    if (slowTimer !== null) {
      clearTimeout(slowTimer);
      slowTimer = null;
    }
  }

  watch(polling.done, (done) => {
    if (done) {
      clearSlow();
      state.value = "ok";
    }
  });
  watch(polling.failures, (failures) => {
    if (failures > 0) {
      clearSlow();
      state.value = "waking";
    }
  });

  function begin(): void {
    clearSlow();
    if (state.value !== "waking") {
      state.value = "checking";
      slowTimer = setTimeout(() => {
        if (state.value === "checking") state.value = "waking";
      }, slowMs);
    }
  }

  if (getCurrentScope()) onScopeDispose(clearSlow);

  return {
    state,
    attempts,
    start() {
      begin();
      polling.start();
    },
    retry() {
      begin();
      polling.refresh();
    },
    reportUnreachable() {
      if (polling.active.value) return;
      state.value = "waking";
      polling.refresh();
    },
  };
}
