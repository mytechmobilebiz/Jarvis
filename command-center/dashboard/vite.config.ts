/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import pkg from "./package.json";

declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  plugins: [
    react(),
    {
      // /version.json lets scripts/smoke.ps1 confirm which dashboard build is being served.
      name: "version-file",
      generateBundle() {
        this.emitFile({ type: "asset", fileName: "version.json", source: JSON.stringify({ version: pkg.version }) + "\n" });
      },
    },
  ],
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  server: {
    port: 5173,
    // CC_DEV_API: e.g. http://127.0.0.1:8090 to develop against a running stack (its nginx proxies /api).
    proxy: { "/api": { target: process.env.CC_DEV_API ?? "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: { outDir: "dist", sourcemap: false },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
