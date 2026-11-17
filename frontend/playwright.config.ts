import { defineConfig, devices } from "@playwright/test";

// Runs without a backend: every API call goes to http://api.test, which the specs answer
// with page.route (e2e/mocks.ts). The dev server is built with that origin as VITE_API_URL.
const PORT = 5179;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./e2e/.results",
  fullyParallel: true,
  forbidOnly: true,
  retries: 0,
  reporter: "list",
  timeout: 30_000,
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "off",
    timezoneId: "UTC",
    locale: "en-GB",
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 },
    },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
  webServer: {
    command: `npx vite --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false,
    env: { VITE_API_URL: "http://api.test" },
    stdout: "ignore",
    stderr: "pipe",
  },
});
