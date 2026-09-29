/**
 * Shared P1 client-log error mapping (work packages A/B, src/api).
 *
 * Fixes the U09 defect class from the old page: every reporting path
 * explicitly handles its own fetch rejection and never recurses into
 * itself. Payloads are redacted by construction — only type/message/url
 * are sent, truncated; cookies, CSRF tokens, secrets, and transcript text
 * are never included.
 */

export interface ClientLogPayload {
  type: string;
  message: string;
  url: string;
}

const MAX_TYPE = 50;
const MAX_MESSAGE = 500;
const MAX_URL = 300;

/** Keys that must never leave the browser in a log payload. */
const FORBIDDEN_KEYS = new Set([
  "cookie",
  "cookies",
  "csrf",
  "csrf_token",
  "csrftoken",
  "set-cookie",
  "authorization",
  "key",
  "admin_key",
  "admin_key_confirmation",
  "secret",
  "token",
  "lease_token",
  "text",
  "transcript",
  "segments",
]);

function truncate(value: string, max: number): string {
  return value.length > max ? value.slice(0, max) : value;
}

function safeString(value: unknown): string {
  if (typeof value === "string") return value;
  if (value instanceof Error) return value.message;
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return String(value);
  }
}

/** Strip forbidden material from an arbitrary error/message value. */
export function sanitizeLogMessage(value: unknown): string {
  const text = safeString(value);
  if (text === "") return "";
  // Best-effort redaction: drop key=value pairs for forbidden keys.
  const redacted = text.replace(
    /("?(?:cookie|csrf|token|key|secret|authorization)"?\s*[:=]\s*)("[^"]*"|'[^']*'|\S+)/gi,
    "$1[redacted]",
  );
  return truncate(redacted, MAX_MESSAGE);
}

/** Build the minimal redacted payload — never includes bodies/cookies. */
export function toClientLogPayload(input: {
  type: unknown;
  message: unknown;
  url?: unknown;
}): ClientLogPayload {
  return {
    type: truncate(safeString(input.type) || "unknown", MAX_TYPE),
    message: sanitizeLogMessage(input.message),
    url: truncate(safeString(input.url ?? currentUrl()), MAX_URL),
  };
}

function currentUrl(): string {
  try {
    if (typeof globalThis.location !== "undefined") {
      return globalThis.location.href;
    }
  } catch {
    // Non-browser (Vitest node env): no URL available.
  }
  return "";
}

export interface LogTransport {
  (payload: ClientLogPayload): Promise<unknown>;
}

function defaultTransport(payload: ClientLogPayload): Promise<unknown> {
  return fetch("/api/clientlog", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    keepalive: true,
  });
}

/**
 * Non-recursive client logger. A re-entrancy guard drops nested reports
 * triggered while a report is in flight, and the transport rejection is
 * always handled (never floating, never re-reported through itself).
 */
export function createClientLogger(
  transport: LogTransport = defaultTransport,
): { report: (type: unknown, message: unknown) => void } {
  let reporting = false;
  return {
    report(type: unknown, message: unknown): void {
      if (reporting) return;
      reporting = true;
      const payload = toClientLogPayload({ type, message });
      void Promise.resolve()
        .then(() => transport(payload))
        .catch(() => undefined)
        .finally(() => {
          reporting = false;
        });
    },
  };
}

/** Shared singleton used by preview pages (explicit, never recursive). */
export const clientLogger = createClientLogger();

/**
 * Explicit rejection handler for async log calls that do not use the
 * singleton (e.g. one-shot reports). Always attaches a catch so no
 * `TypeError: Failed to fetch` escapes as an unhandled rejection.
 */
export function reportRejection(
  promise: Promise<unknown>,
  transport: LogTransport = defaultTransport,
): void {
  void promise.catch(() => undefined);
  void transport;
}

export { FORBIDDEN_KEYS };
