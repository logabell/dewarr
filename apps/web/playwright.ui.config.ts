import { defineConfig } from "@playwright/test";
import shared from "./playwright.shared";

// Fully mocked browser journeys only: no PostgreSQL, Python API, migrations, or worker.
export default defineConfig({
  ...shared,
  testDir: "./tests/ui",
  reporter: [["list"], ["junit", { outputFile: "test-results/ui.xml" }]],
  outputDir: "test-results/ui",
  use: { ...shared.use, baseURL: "http://127.0.0.1:4173" },
  webServer: {
    command:
      "node node_modules/vite/bin/vite.js preview --config vite.ui.config.ts --host 127.0.0.1 --port 4173 --strictPort",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
    timeout: 15_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 3000 },
  },
});
