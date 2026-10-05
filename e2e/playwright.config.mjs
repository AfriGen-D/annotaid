// Browser tests for AnnotAid v2. Each worker starts its own server on a free
// port with a throwaway data directory (see lib/fixtures.mjs), so test files
// run in parallel without sharing state.
//
//   cd e2e && npm install && npx playwright install chromium
//   npx playwright test                       # everything
//   npx playwright test tests/curator.spec.mjs
//   E2E_KEYS=../../.keys npx playwright test  # also run real AI extraction (free model)
//
// Test titles start with the case id from the test plan (e.g. "CU-05 …"),
// so results map straight back onto it (results.json).
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 120_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: process.env.CI ? 2 : 4,
  retries: 0,
  reporter: [["list"], ["json", { outputFile: "results.json" }], ["html", { open: "never" }]],
  use: {
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 15_000,
  },
});
