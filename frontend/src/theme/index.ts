/**
 * Shared theme entry point. `RoleTheme.tsx` stays component-only so the
 * fast-refresh lint rule passes; shared values are re-exported from here.
 */
export { RoleTheme } from "./RoleTheme";
export { adminTheme, annotatorTheme } from "./antdThemes";
export { readCspNonce } from "./csp";
export { paletteFor, type ThemeRole } from "./tokens";
