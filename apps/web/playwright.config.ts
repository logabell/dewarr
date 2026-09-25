import { defineConfig } from "@playwright/test";
import shared from "./playwright.shared";

export default defineConfig({
  ...shared,
  testDir: "./tests",
  testIgnore: "**/ui/**",
  projects: [
    { name: "foundation", testMatch: "foundation.spec.ts" },
    {
      name: "browser",
      testIgnore: ["foundation.spec.ts", "**/ui/**"],
      dependencies: ["foundation"],
    },
  ],
  reporter: [["list"], ["junit", { outputFile: "test-results/browser.xml" }]],
  use: {
    ...shared.use,
    baseURL: "http://127.0.0.1:8001",
  },
  webServer: {
    command: "uv run --no-sync python scripts/e2e_server.py",
    cwd: "../..",
    url: "http://127.0.0.1:8001/api/health/ready",
    reuseExistingServer: false,
    timeout: 30_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },
  },
});
