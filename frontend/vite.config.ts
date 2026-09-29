import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { defineConfig } from "vite";

const isP1Build = process.env.FRONTEND_BUILD === "p1";
const rootDir = path.dirname(fileURLToPath(import.meta.url));

// Shared admin and preview build.
// - production assets use /frontend/ for delivery by Flask.
// - hashed files live under assets/; HTML uses absolute /frontend/ URLs.
// - manifest (.vite/manifest.json) backs Flask integrity checks.
// - production optimised for both dist/ and dist-p1/; only the P1 build
//   bundles the dev component matrix (guarded by __FRONTEND_BUILD_P1__).
// - sourcemaps are off: server must reject .map requests.
// The Vitest `test` section is kept here so `npm test` runs pure-logic
// suites from src/test/** without a separate config. The F harness keeps
// its own vite.harness.config.ts (base /p1h/) and is untouched.
export default defineConfig(({ command }) => ({
  plugins: [react()],
  base: command === "serve" ? "/" : "/frontend/",
  server: {
    host: "127.0.0.1",
    proxy: Object.fromEntries(
      ["/api", "/static", "/login.html"].map((prefix) => [
        prefix,
        {
          target: process.env.ANNOTATION_DEV_API_URL || "http://127.0.0.1:8088",
          changeOrigin: false,
        },
      ]),
    ),
  },
  define: {
    __FRONTEND_BUILD_P1__: JSON.stringify(isP1Build),
  },
  resolve: {
    // CSP-nonce adapter for rc-level dynamic style injection
    // (see src/theme/rcDynamicCss.ts). The bare specifier is used by
    // cssinjs; the relative specifier is used inside rc-util itself.
    // The wrapper imports the real file with an explicit `.js` extension
    // so neither alias matches it back.
    alias: [
      {
        find: "@rc-component/util/es/Dom/dynamicCSS",
        replacement: path.resolve(rootDir, "src/theme/rcDynamicCss.ts"),
      },
      {
        find: /^\.\/Dom\/dynamicCSS$/,
        replacement: path.resolve(rootDir, "src/theme/rcDynamicCss.ts"),
      },
    ],
  },
  build: {
    outDir: isP1Build ? "dist-p1" : "dist",
    emptyOutDir: true,
    sourcemap: false,
    manifest: true,
    assetsDir: "assets",
    chunkSizeWarningLimit: 1024,
  },
  test: {
    environment: "node",
    include: ["src/test/**/*.test.ts"],
  },
}));
