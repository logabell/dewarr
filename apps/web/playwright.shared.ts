import { defineConfig } from "@playwright/test";

export default defineConfig({
  workers: 1,
  fullyParallel: false,
  retries: 0,
  maxFailures: process.env.CI ? 0 : 1,
  globalTimeout: 10 * 60_000,
  use: {
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
    // Recording every passing journey was expensive. Enable traces for a focused diagnosis.
    trace: process.env.BOOK_TEST_TRACE === "1" ? "retain-on-failure" : "off",
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },
});
