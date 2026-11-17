import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [vue()],
  // Unit tests only; the Playwright specs in e2e/ run with `npm run e2e`.
  test: { environment: "jsdom", include: ["src/**/*.test.ts"] },
});
