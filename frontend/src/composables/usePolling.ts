// Polls a task on a timer (phase 3, "Flow": every 2-3 s while an invoice is processing).
// One request at a time: the next one is scheduled only after the previous one settled.
// Stops when isDone() says the value is terminal, pauses while the tab is hidden (and polls
// at once when it is visible again), and backs off exponentially on consecutive errors.

import { getCurrentScope, onScopeDispose, ref, shallowRef, type Ref } from "vue";

export interface PollingOptions<T> {
  intervalMs?: number;
  maxBackoffMs?: number;
  isDone?: (value: T) => boolean;
}

export interface Polling<T> {
  data: Readonly<Ref<T | null>>;
  error: Readonly<Ref<unknown>>;
  failures: Readonly<Ref<number>>;
  // Started and not finished (a hidden tab pauses it without making it inactive).
  active: Readonly<Ref<boolean>>;
  done: Readonly<Ref<boolean>>;
  start(): void;
  stop(): void;
  refresh(): void;
}

export function nextDelay(intervalMs: number, maxBackoffMs: number, failures: number): number {
  if (failures <= 0) return intervalMs;
  return Math.min(intervalMs * 2 ** Math.min(failures, 30), maxBackoffMs);
}

function tabHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function usePolling<T>(task: () => Promise<T>, options: PollingOptions<T> = {}): Polling<T> {
  const intervalMs = options.intervalMs ?? 2500;
  const maxBackoffMs = options.maxBackoffMs ?? 30000;
  const isDone = options.isDone ?? (() => false);

  const data = shallowRef<T | null>(null);
  const error = shallowRef<unknown>(null);
  const failures = ref(0);
  const active = ref(false);
  const done = ref(false);

  let timer: ReturnType<typeof setTimeout> | null = null;
  let inFlight = false;
  // Bumped by stop() and refresh(): an answer from an older generation is dropped.
  let generation = 0;

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function schedule(): void {
    clearTimer();
    if (!active.value || tabHidden()) return;
    timer = setTimeout(run, nextDelay(intervalMs, maxBackoffMs, failures.value));
  }

  async function run(): Promise<void> {
    timer = null;
    if (!active.value || inFlight || tabHidden()) return;
    inFlight = true;
    const mine = generation;
    try {
      const value = await task();
      if (mine !== generation) return;
      data.value = value;
      error.value = null;
      failures.value = 0;
      if (isDone(value)) {
        done.value = true;
        active.value = false;
      }
    } catch (err) {
      if (mine !== generation) return;
      error.value = err;
      failures.value += 1;
    } finally {
      if (mine === generation) inFlight = false;
    }
    schedule();
  }

  function start(): void {
    if (active.value) return;
    active.value = true;
    done.value = false;
    void run();
  }

  function stop(): void {
    generation += 1;
    inFlight = false;
    active.value = false;
    clearTimer();
  }

  function refresh(): void {
    stop();
    start();
  }

  function onVisibility(): void {
    if (tabHidden()) clearTimer();
    else if (active.value && timer === null && !inFlight) void run();
  }

  if (typeof document !== "undefined") {
    document.addEventListener("visibilitychange", onVisibility);
  }
  if (getCurrentScope()) {
    onScopeDispose(() => {
      stop();
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibility);
      }
    });
  }

  // Readonly in the types only: the screens read these, only this composable writes them.
  return {
    data,
    error,
    failures,
    active,
    done,
    start,
    stop,
    refresh,
  };
}
