/**
 * Reads the per-response CSP nonce Flask injects into
 * `<meta name="csp-nonce">`. The value is passed to Ant Design's
 * ConfigProvider (`csp={{ nonce }}`) so runtime-injected
 * `<style nonce="…">` elements satisfy `style-src 'self' 'nonce-…'`
 * without `unsafe-inline`. Returns undefined when absent (dev server,
 * tests) so ConfigProvider falls back to its default behavior.
 */
export function readCspNonce(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const content = document
    .querySelector('meta[name="csp-nonce"]')
    ?.getAttribute("content");
  if (!content) return undefined;
  const value = content.trim();
  // Unrendered Vite placeholder or empty value: no nonce available.
  if (value === "" || value.includes("%")) return undefined;
  return value;
}
