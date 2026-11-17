// The app-wide health monitor, created once by App.vue (whose scope lives as long as the
// page) and handed to the screens, which report a request that found the server unreachable.

import { inject, provide, ref, type InjectionKey } from "vue";

import type { HealthMonitor } from "./useHealth";

const KEY: InjectionKey<HealthMonitor> = Symbol("server-health");

export function provideServerHealth(monitor: HealthMonitor): void {
  provide(KEY, monitor);
}

const DETACHED: HealthMonitor = {
  state: ref("ok"),
  attempts: ref(0),
  start: () => {},
  retry: () => {},
  reportUnreachable: () => {},
};

export function useServerHealth(): HealthMonitor {
  return inject(KEY, DETACHED);
}
