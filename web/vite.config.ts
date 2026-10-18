/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the SPA runs on 5273 and proxies /api to the FastAPI backend on 8700 (same origin for the refresh cookie).
export default defineConfig({
  plugins: [react()],
  server: { port: 5273, host: "127.0.0.1", proxy: { "/api": { target: "http://127.0.0.1:8700", changeOrigin: false } } },
  test: { environment: "jsdom", globals: true, setupFiles: ["./src/test/setup.ts"], css: false },
});
