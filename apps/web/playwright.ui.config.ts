import { defineConfig } from "@playwright/test";
import shared from "./playwright.shared";

const port = Number(process.env.BOOK_UI_PORT || 4173);
if (!Number.isInteger(port) || port < 1024 || port > 65535)
  throw new Error("BOOK_UI_PORT must be a port between 1024 and 65535");
const baseURL = `http://127.0.0.1:${port}`;

// Fully mocked browser journeys only: no PostgreSQL, Python API, migrations, or worker.
export default defineConfig({
  ...shared,
  testDir: "./tests/ui",
  reporter: [["list"], ["junit", { outputFile: "test-results/ui.xml" }]],
  outputDir: "test-results/ui",
  use: { ...shared.use, baseURL },
  webServer: {
    command: `node node_modules/vite/bin/vite.js preview --config vite.ui.config.ts --host 127.0.0.1 --port ${port} --strictPort`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 15_000,
    gracefulShutdown: { signal: "SIGTERM", timeout: 3000 },
  },
});
