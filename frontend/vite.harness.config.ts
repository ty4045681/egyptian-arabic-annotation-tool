import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Test-harness build for work package F browser tests (tests/browser/
// test_frontend_p1_*.py). This is NOT the canonical P1 preview entry:
// packages A/B own the production build and Flask preview serving. The
// harness bundles only the workspace feature plus a minimal test-only page
// shell so the ten persistence scenarios and layout checks can run against
// the real Flask API before package B lands.
export default defineConfig({
  plugins: [react()],
  root: "src/features/workspace/preview",
  base: "/p1h/",
  build: {
    outDir: "../../../../dist-harness",
    emptyOutDir: true,
    sourcemap: false,
    manifest: false,
    assetsDir: "assets",
  },
});
