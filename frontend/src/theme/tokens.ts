/**
 * Shared design tokens — the single source of truth.
 *
 * This module feeds BOTH:
 *  - the Ant Design theme configs (`antdThemes.ts`), and
 *  - the business CSS variables in `tokens.css`, which is **generated** from
 *    this file by `frontend/scripts/generate-tokens.mjs`
 *    (`npm run gen:tokens`). `npm run check:tokens` (wired into `check:dist`)
 *    fails when the generated file drifts, so the two cannot silently
 *    diverge. Do not hand-edit `tokens.css`.
 *
 * Pages must not copy their own color tables: `RoleTheme` applies the same
 * semantic tokens in both role themes. No theme switching in P1; the role
 * entry selects the theme.
 */

/** 4px base; only these steps are allowed in business layouts. */
export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
} as const;

export const pageGutterDesktop = 24;
export const pageGutterSmall = 16;

/** Control heights: one height per toolbar row. */
export const controlHeight = 32;
export const controlHeightTouch = 44;

export const radiusControl = 6;
export const radiusContainer = 8;

export const fontSizeUI = 14;
export const fontSizeAux = 12;
export const fontSizeTranscript = 18;
export const fontSizeTranscriptMin = 14;
export const fontSizeTranscriptMax = 32;

/** System stack only: no remote fonts, no CDN. Tabular numerals. */
export const fontFamily =
  "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, " +
  "'Noto Sans', sans-serif, 'Apple Color Emoji', 'Segoe UI Emoji'";

export type ThemeRole = "admin" | "annotator";

/**
 * Semantic colors. Both roles map the same names; exact values differ per
 * theme. Plain-text contrast target is 4.5:1.
 */
export interface SemanticPalette {
  text: string;
  textSecondary: string;
  background: string;
  surface: string;
  surfaceAlt: string;
  border: string;
  borderStrong: string;
  primary: string;
  primarySoft: string;
  success: string;
  warning: string;
  warningSoft: string;
  danger: string;
  info: string;
  neutral: string;
}

export const adminPalette: SemanticPalette = {
  text: "#1e293b",
  textSecondary: "#64748b",
  background: "#f8fafc",
  surface: "#ffffff",
  surfaceAlt: "#fafafa",
  border: "#e2e8f0",
  borderStrong: "#cbd5e1",
  primary: "#4f46e5",
  primarySoft: "#eef2ff",
  success: "#1a7f37",
  warning: "#9a6700",
  warningSoft: "#fef0c7",
  danger: "#b42318",
  info: "#0969da",
  neutral: "#475467",
};

export const adminDarkPalette: SemanticPalette = {
  text: "#e2e8f0", textSecondary: "#94a3b8", background: "#0f172a", surface: "#172033",
  surfaceAlt: "#1e293b", border: "#334155", borderStrong: "#475569", primary: "#a5b4fc",
  primarySoft: "#282b50", success: "#4ade80", warning: "#fbbf24", warningSoft: "#3a2d16",
  danger: "#fca5a5", info: "#93c5fd", neutral: "#94a3b8",
};

export const annotatorPalette: SemanticPalette = {
  text: "rgba(255, 255, 255, 0.88)",
  textSecondary: "rgba(255, 255, 255, 0.55)",
  background: "#141414",
  surface: "#1f1f1f",
  surfaceAlt: "#262626",
  border: "#434343",
  borderStrong: "#595959",
  primary: "#177ddc",
  primarySoft: "rgba(23, 125, 220, 0.24)",
  success: "#49aa19",
  warning: "#d89614",
  warningSoft: "rgba(216, 150, 20, 0.22)",
  danger: "#dc4446",
  info: "#177ddc",
  neutral: "rgba(255, 255, 255, 0.55)",
};

export function paletteFor(role: ThemeRole): SemanticPalette {
  return role === "admin" ? adminPalette : annotatorPalette;
}

/** Structured form used by the generator (see generate-tokens.mjs). */
export const tokenSource = {
  spacing,
  pageGutterDesktop,
  pageGutterSmall,
  controlHeight,
  controlHeightTouch,
  radiusControl,
  radiusContainer,
  fontSizeUI,
  fontSizeAux,
  fontSizeTranscript,
  fontFamily,
  adminPalette,
  adminDarkPalette,
  annotatorPalette,
};
