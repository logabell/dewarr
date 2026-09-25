import { defineConfig } from "vite";

// Do not inherit the development server's /api proxy to port 8000.
// Every API response in tests/ui must come from the browser's mocked routes.
export default defineConfig({ preview: { proxy: {} } });
