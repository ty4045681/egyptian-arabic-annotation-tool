/**
 * P1 dist integrity check (work packages A/B).
 *
 * Validates the Vite production outputs used by Flask preview serving:
 * - index.html exists and references absolute /frontend/ asset URLs.
 * - .vite/manifest.json exists, parses, and every listed file/css/import
 *   exists on disk (recursive chunk verification, including dynamic imports).
 * - Every hashed asset referenced from HTML exists; CSS files exist.
 * - No source maps are emitted (sourcemap off; server must reject .map).
 * - Emits a version/inventory manifest (node/npm versions, package version,
 *   lockfile hash, asset sizes).
 *
 * Usage:
 *   node scripts/check-dist.mjs               # check dist/ and dist-p1/
 *   node scripts/check-dist.mjs dist          # check dist/ only
 *   node scripts/check-dist.mjs dist-p1       # check dist-p1/ only
 *   node scripts/check-dist.mjs --dir dist    # same, explicit
 */
import { createHash } from "node:crypto";
import { execSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const frontendDir = resolve(here, "..");

function parseArgs(argv) {
  const dirs = [];
  for (const arg of argv.slice(2)) {
    if (arg === "--dir") continue;
    if (arg.startsWith("--")) continue;
    if (arg === "dist" || arg === "dist-p1" || arg === "./dist" || arg === "./dist-p1") {
      dirs.push(arg.replace("./", ""));
    } else if (arg === "dist/" || arg === "dist-p1/") {
      dirs.push(arg.replace(/\/$/, ""));
    } else {
      console.error(`Unknown argument: ${arg} (expected dist and/or dist-p1)`);
      process.exit(2);
    }
  }
  // Support `--dir dist` form.
  const dirFlag = argv.indexOf("--dir");
  if (dirFlag !== -1 && argv[dirFlag + 1]) {
    const value = argv[dirFlag + 1].replace("./", "").replace(/\/$/, "");
    if (value === "dist" || value === "dist-p1") dirs.push(value);
  }
  return dirs.length > 0 ? [...new Set(dirs)] : ["dist", "dist-p1"];
}

function sha256File(path) {
  const data = readFileSync(path);
  return createHash("sha256").update(data).digest("hex").slice(0, 16);
}

function walkFiles(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) walkFiles(full, out);
    else out.push(full);
  }
  return out;
}

function checkDist(name) {
  const errors = [];
  const warnings = [];
  const distDir = join(frontendDir, name);
  console.log(`\n=== ${name}/ ===`);

  if (!existsSync(distDir) || !statSync(distDir).isDirectory()) {
    return { name, ok: false, errors: [`missing directory: ${name}/`], warnings, assets: [] };
  }

  // 1. HTML entry.
  const htmlPath = join(distDir, "index.html");
  if (!existsSync(htmlPath)) {
    errors.push("missing index.html");
  } else {
    const html = readFileSync(htmlPath, "utf8");
    if (!html.includes("/frontend/")) {
      errors.push("index.html does not reference absolute /frontend/ URLs");
    }
    // Collect /frontend/assets/... references.
    const refs = new Set();
    for (const match of html.matchAll(/\/frontend\/assets\/[A-Za-z0-9._-]+\.[A-Za-z0-9]+/g)) {
      refs.add(match[0]);
    }
    for (const ref of refs) {
      const rel = ref.replace("/frontend/", "");
      if (!existsSync(join(distDir, rel))) {
        errors.push(`HTML references missing asset: ${ref}`);
      }
    }
    if (!/<script[^>]+type="module"/.test(html)) {
      warnings.push("index.html has no module script tag (unexpected for Vite build)");
    }
  }

  // 2. Manifest.
  const manifestPath = join(distDir, ".vite", "manifest.json");
  if (!existsSync(manifestPath)) {
    errors.push("missing .vite/manifest.json (Vite manifest:true required)");
  }
  let manifest = null;
  if (existsSync(manifestPath)) {
    try {
      manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
    } catch (error) {
      errors.push(`manifest is not valid JSON: ${String(error)}`);
    }
  }
  const seen = new Set();
  if (manifest) {
    const keys = Object.keys(manifest);
    if (keys.length === 0) errors.push("manifest is empty");
    const visit = (key) => {
      if (seen.has(key)) return;
      seen.add(key);
      const entry = manifest[key];
      if (!entry || typeof entry.file !== "string") {
        errors.push(`manifest entry ${JSON.stringify(key)} has no file`);
        return;
      }
      if (!existsSync(join(distDir, entry.file))) {
        errors.push(`manifest file missing: ${entry.file} (entry ${key})`);
      }
      for (const css of entry.css ?? []) {
        if (!existsSync(join(distDir, css))) {
          errors.push(`manifest css missing: ${css} (entry ${key})`);
        }
      }
      for (const asset of entry.assets ?? []) {
        if (!existsSync(join(distDir, asset))) {
          errors.push(`manifest asset missing: ${asset} (entry ${key})`);
        }
      }
      for (const imported of entry.imports ?? []) {
        if (manifest[imported] === undefined) {
          // Dynamic/static import target must be a manifest key; otherwise
          // the chunk graph is incomplete.
          const candidate = join(distDir, imported);
          if (!existsSync(candidate)) {
            errors.push(`manifest import missing: ${imported} (entry ${key})`);
          }
        } else {
          visit(imported);
        }
      }
      for (const dyn of entry.dynamicImports ?? []) {
        if (manifest[dyn] === undefined) {
          const candidate = join(distDir, dyn);
          if (!existsSync(candidate)) {
            errors.push(`manifest dynamicImport missing: ${dyn} (entry ${key})`);
          }
        } else {
          visit(dyn);
        }
      }
    };
    for (const key of keys) visit(key);
  }

  // 3. Assets directory + sourcemap check.
  const assetsDir = join(distDir, "assets");
  if (!existsSync(assetsDir)) {
    errors.push("missing assets/ directory");
  } else {
    const files = walkFiles(assetsDir);
    if (files.length === 0) errors.push("assets/ is empty");
    const hasJs = files.some((f) => f.endsWith(".js"));
    const hasCss = files.some((f) => f.endsWith(".css"));
    if (!hasJs) errors.push("assets/ has no .js chunk");
    if (!hasCss) warnings.push("assets/ has no .css (ok if no styles emitted yet)");
    const maps = files.filter((f) => f.endsWith(".map"));
    if (maps.length > 0) {
      errors.push(`sourcemaps must be off, found: ${maps.slice(0, 5).join(", ")}`);
    }
    for (const file of files) {
      const base = file.split("/").pop() ?? "";
      if (base.startsWith(".")) {
        errors.push(`hidden file in assets/: ${base}`);
      }
    }
  }

  // 4. No stray maps at top level either.
  if (existsSync(distDir)) {
    const topMaps = walkFiles(distDir).filter((f) => f.endsWith(".map"));
    if (topMaps.length > 0 && !errors.some((e) => e.includes("sourcemaps"))) {
      errors.push(`sourcemaps must be off, found ${topMaps.length} .map file(s)`);
    }
  }

  // 5. Inventory.
  const assets = [];
  if (existsSync(distDir)) {
    for (const file of walkFiles(distDir)) {
      const stat = statSync(file);
      if (stat.isFile()) {
        const rel = file.slice(distDir.length + 1);
        if (rel.startsWith(".vite/")) continue;
        assets.push({ path: rel, bytes: stat.size, sha16: sha256File(file) });
      }
    }
    assets.sort((a, b) => (a.path < b.path ? -1 : 1));
    const total = assets.reduce((n, a) => n + a.bytes, 0);
    console.log(`files: ${assets.length}, total bytes: ${total}`);
    for (const asset of assets.slice(0, 50)) {
      console.log(`  ${String(asset.bytes).padStart(8)}  ${asset.sha16}  ${asset.path}`);
    }
    if (assets.length > 50) console.log(`  ... and ${assets.length - 50} more`);
  }

  const ok = errors.length === 0;
  for (const error of errors) console.error(`ERROR [${name}]: ${error}`);
  for (const warning of warnings) console.warn(`WARN  [${name}]: ${warning}`);
  console.log(ok ? `OK ${name}/` : `FAIL ${name}/ (${errors.length} error(s))`);
  return { name, ok, errors, warnings, assets };
}

function main() {
  const targets = parseArgs(process.argv);
  const pkg = JSON.parse(readFileSync(join(frontendDir, "package.json"), "utf8"));
  console.log(`package: ${pkg.name}@${pkg.version}`);
  const results = targets.map(checkDist);
  // Version/inventory manifest.
  const lockPath = join(frontendDir, "package-lock.json");
  let lockHash = "missing";
  if (existsSync(lockPath)) lockHash = sha256File(lockPath);
  console.log("\n--- version inventory ---");
  console.log(`node: ${process.version}`);
  try {
    console.log(`npm: ${execSync("npm --version").toString().trim()}`);
  } catch {
    console.log("npm: unknown");
  }
  console.log(`package: ${pkg.name}@${pkg.version}`);
  console.log(`lockfile sha16: ${lockHash}`);
  try {
    console.log(`git: ${execSync("git rev-parse --short HEAD", { cwd: frontendDir }).toString().trim()}`);
  } catch {
    console.log("git: unknown");
  }
  for (const result of results) {
    console.log(`${result.name}/: ${result.ok ? "OK" : "FAIL"} (${result.assets.length} files)`);
  }
  if (results.some((r) => !r.ok)) process.exit(1);
}

main();
