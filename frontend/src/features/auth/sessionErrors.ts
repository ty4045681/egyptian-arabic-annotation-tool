/**
 * Client error reporting (work package F, features/auth).
 *
 * Fixes the U09 defect from the old page (index.html reportError wrapped
 * fetch in a sync try/catch but never handled the promise rejection, which
 * produced an unhandled `TypeError: Failed to fetch` on offline saves):
 * every reporting path explicitly handles rejection and never recurses into
 * itself. Payloads are redacted by construction — only type/message/url are
 * sent, truncated; cookies, CSRF tokens, secrets and transcript text are
 * never included.
 */

export interface ClientLogPayload {
  type: string;
  message: string;
  url: string;
}

const MAX_MESSAGE = 500;
const MAX_URL = 300;
const MAX_TYPE = 50;

function truncate(value: string, max: number): string {
  return value.length > max ? value.slice(0, max) : value;
}

export function sanitizeClientLog(input: {
  type: unknown;
  message: unknown;
  url?: unknown;
}): ClientLogPayload {
  return {
    type: truncate(String(input.type ?? "unknown"), MAX_TYPE),
    message: truncate(String(input.message ?? ""), MAX_MESSAGE),
    url: truncate(String(input.url ?? ""), MAX_URL),
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: Record<string, unknown>;

  constructor(message: string, status: number, body: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export function classifyFailure(status: number, code?: string): "auth" | "conflict" | "transient" | "terminal" {
  if (status === 0) return "transient";
  if (status === 401) return "auth";
  if (status === 403 && code === "account_deactivated") return "auth";
  if (status === 409) return "conflict";
  if (status === 400 || status === 403 || status === 404 || status === 422) return "terminal";
  return "transient";
}

export interface LogTransport {
  (payload: ClientLogPayload): Promise<unknown>;
}

function defaultTransport(payload: ClientLogPayload): Promise<unknown> {
  return (globalThis as unknown as { fetch: typeof fetch }).fetch("/api/clientlog", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    keepalive: true,
  });
}

/**
 * Report a client error without ever producing an unhandled rejection and
 * without recursing: failures of the transport itself are swallowed, and a
 * re-entrancy guard drops nested reports triggered while reporting.
 */
export function createClientLogger(transport: LogTransport = defaultTransport): {
  report: (type: unknown, message: unknown) => void;
} {
  let reporting = false;
  return {
    report(type: unknown, message: unknown): void {
      if (reporting) return;
      reporting = true;
      const payload = sanitizeClientLog({
        type,
        message,
        url: typeof globalThis.location !== "undefined" ? globalThis.location.href : "",
      });
      void Promise.resolve()
        .then(() => transport(payload))
        .catch(() => undefined)
        .finally(() => {
          reporting = false;
        });
    },
  };
}
