/**
 * Generate `src/theme/tokens.css` from `src/theme/tokens.ts` (single source).
 *
 * Usage:
 *   node scripts/generate-tokens.mjs            # write tokens.css
 *   node scripts/generate-tokens.mjs --check     # fail if tokens.css drifts
 *
 * `npm run gen:tokens` writes; `npm run check:tokens` checks (wired into
 * `check:dist` and CI so the checked-in CSS cannot silently diverge from the
 * TypeScript source). The TS module is bundled to a temp file with esbuild
 * (a Vite dependency) and imported, so there is exactly one definition.
 */
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const frontendDir = resolve(here, "..");
const sourcePath = join(frontendDir, "src", "theme", "tokens.ts");
const targetPath = join(frontendDir, "src", "theme", "tokens.css");
const checkOnly = process.argv.includes("--check");

const tmp = mkdtempSync(join(tmpdir(), "p1-tokens-"));
const bundlePath = join(tmp, "tokens.mjs");
try {
  execFileSync(
    join(frontendDir, "node_modules", ".bin", "esbuild"),
    [sourcePath, "--bundle", "--platform=node", "--format=esm", `--outfile=${bundlePath}`],
    { stdio: "inherit" },
  );
  const mod = await import(pathToFileURL(bundlePath).href);
  const generated = render(mod.tokenSource);
  const current = (() => {
    try {
      return readFileSync(targetPath, "utf8");
    } catch {
      return null;
    }
  })();
  if (checkOnly) {
    if (current !== generated) {
      console.error(
        "tokens.css is out of date. Run `npm --prefix frontend run gen:tokens`.",
      );
      process.exit(1);
    }
    console.log("tokens.css matches tokens.ts");
  } else {
    writeFileSync(targetPath, generated);
    console.log(`wrote ${targetPath}`);
  }
} finally {
  rmSync(tmp, { recursive: true, force: true });
}

function cssVarLines(palette) {
  return [
    ["--app-background", palette.background],
    ["--app-surface", palette.surface],
    ["--app-surface-alt", palette.surfaceAlt],
    ["--app-text", palette.text],
    ["--app-text-secondary", palette.textSecondary],
    ["--app-border", palette.border],
    ["--app-border-strong", palette.borderStrong],
    ["--app-primary", palette.primary],
    ["--app-primary-soft", palette.primarySoft],
    ["--app-success", palette.success],
    ["--app-warning", palette.warning],
    ["--app-warning-soft", palette.warningSoft],
    ["--app-danger", palette.danger],
    ["--app-info", palette.info],
    ["--app-neutral", palette.neutral],
  ]
    .map(([name, value]) => `  ${name}: ${value};`)
    .join("\n");
}

function render(src) {
  const shared = [
    `  --space-xs: ${src.spacing.xs}px;`,
    `  --space-sm: ${src.spacing.sm}px;`,
    `  --space-md: ${src.spacing.md}px;`,
    `  --space-lg: ${src.spacing.lg}px;`,
    `  --space-xl: ${src.spacing.xl}px;`,
    `  --space-xxl: ${src.spacing.xxl}px;`,
    `  --page-gutter: ${src.pageGutterDesktop}px;`,
    `  --control-height: ${src.controlHeight}px;`,
    `  --control-height-touch: ${src.controlHeightTouch}px;`,
    `  --radius-control: ${src.radiusControl}px;`,
    `  --radius-container: ${src.radiusContainer}px;`,
    `  --font-size-ui: ${src.fontSizeUI}px;`,
    `  --font-size-aux: ${src.fontSizeAux}px;`,
    `  --font-size-transcript: ${src.fontSizeTranscript}px;`,
    `  --font-family: ${src.fontFamily};`,
  ].join("\n");
  return `/* AUTO-GENERATED from src/theme/tokens.ts — do not edit by hand.
 * Regenerate: npm --prefix frontend run gen:tokens
 * Drift check: npm --prefix frontend run check:tokens */

*,
*::before,
*::after {
  box-sizing: border-box;
}

body {
  margin: 0;
}

audio,
canvas,
img,
video {
  max-width: 100%;
}

:root,
[data-theme="admin"] {
${shared}
${cssVarLines(src.adminPalette)}
}

[data-theme="annotator"] {
${cssVarLines(src.annotatorPalette)}
}

:root[data-admin-mode="dark"],
[data-theme="admin"][data-color-mode="dark"] {
${cssVarLines(src.adminDarkPalette)}
}

body {
  background: var(--app-background);
  color: var(--app-text);
  font-family: var(--font-family);
  font-size: var(--font-size-ui);
  line-height: 1.5;
}

@media (max-width: 768px) {
  :root,
  [data-theme="admin"],
  [data-theme="annotator"] {
    --page-gutter: ${src.pageGutterSmall}px;
    --control-height: ${src.controlHeightTouch}px;
  }
}
`;
}
