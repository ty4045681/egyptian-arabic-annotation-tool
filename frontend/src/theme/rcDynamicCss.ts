/**
 * CSP-nonce adapter for `@rc-component/util` dynamic style injection.
 *
 * Ant Design's runtime style engine (cssinjs) already forwards the
 * per-response nonce from `RoleTheme`, but two rc-level helpers bypass it:
 * `getScrollBarSize` (used by every Table and by Modal/Drawer scroll
 * locking) calls `updateCSS` with no `csp` option, so the injected
 * `<style>` elements violate the Flask `style-src` policy. Rather than
 * weakening CSP with `unsafe-inline` (forbidden by the P1 plan) this module
 * re-attaches the response nonce from the `<meta name="csp-nonce">` tag and
 * delegates to the real implementation. Behavior is otherwise unchanged;
 * without a nonce meta (dev server, unit tests) it is a transparent
 * pass-through.
 *
 * Wired via `vite.config.ts` aliases covering both the bare specifier used
 * by cssinjs (`@rc-component/util/es/Dom/dynamicCSS`) and the relative
 * specifier used inside rc-util itself (`./Dom/dynamicCSS`). The real
 * module is imported with an explicit `.js` extension so the aliases never
 * match it back.
 */
import {
  clearContainerCache,
  injectCSS as rawInjectCSS,
  removeCSS,
  updateCSS as rawUpdateCSS,
} from "@rc-component/util/es/Dom/dynamicCSS.js";
import { readCspNonce } from "./csp";

type InjectOption = Parameters<typeof rawInjectCSS>[1];
type UpdateOption = Parameters<typeof rawUpdateCSS>[2];

function attachNonce<T>(option: T): T {
  const nonce = readCspNonce();
  if (nonce === undefined) return option;
  const record = (option ?? {}) as Record<string, unknown>;
  const csp = (record["csp"] ?? {}) as Record<string, unknown>;
  if (typeof csp["nonce"] === "string" && csp["nonce"] !== "") return option;
  return { ...record, csp: { ...csp, nonce } } as T;
}

export function injectCSS(css: string, option?: InjectOption): HTMLStyleElement {
  return rawInjectCSS(css, attachNonce(option));
}

export function updateCSS(css: string, key: string, option?: UpdateOption): HTMLElement {
  return rawUpdateCSS(css, key, attachNonce(option));
}

export { clearContainerCache, removeCSS };
