/**
 * Shared P1 HTTP client (work packages A/B, src/api).
 *
 * Same-origin fetch with cookies, explicit abort support, and a bounded
 * retry policy for read-only GETs:
 * - 400/401/403 never retry (auth/client errors).
 * - AbortError never retries and propagates with name "AbortError".
 * - Transient failures retry at most twice after the first attempt.
 * - Window-focus refetch is disabled in the TanStack defaults (see
 *   src/app/providers.tsx); this client never triggers refetches itself.
 * - Callers combine `AbortController` cancellation with a request-key /
 *   attribution check (see queryKeys.ts and
 *   features/admin/corpus/queryCodec.ts `makeCorpusRequestKey` /
 *   `isStaleCorpusResponse`): a late response whose key no longer matches
 *   the current key must be ignored even if the abort arrived late.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string | undefined;
  readonly payload?: unknown;

  constructor(message: string, status: number, payload?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    const code = readErrorCode(payload);
    if (code !== undefined) {
      this.code = code;
    }
    this.payload = payload;
  }
}

function readErrorCode(payload: unknown): string | undefined {
  if (typeof payload === "object" && payload !== null) {
    const value = (payload as Record<string, unknown>).code;
    if (typeof value === "string" && value !== "") return value;
  }
  return undefined;
}

export function isAbortError(error: unknown): boolean {
  return (
    (typeof error === "object" &&
      error !== null &&
      (error as { name?: unknown }).name === "AbortError") ||
    (error instanceof ApiError &&
      error.name === "AbortError")
  );
}

/** Statuses that must never be retried. */
export function isNonRetryableStatus(status: unknown): boolean {
  return status === 400 || status === 401 || status === 403;
}

export function statusOf(error: unknown): number | undefined {
  if (error instanceof ApiError) return error.status;
  if (typeof error === "object" && error !== null) {
    const record = error as Record<string, unknown>;
    const direct = record.status ?? record.statusCode;
    if (typeof direct === "number" && Number.isFinite(direct)) return direct;
    const response = record.response;
    if (typeof response === "object" && response !== null) {
      const nested = (response as Record<string, unknown>).status;
      if (typeof nested === "number" && Number.isFinite(nested)) {
        return nested;
      }
    }
  }
  return undefined;
}

/**
 * Read-only GET retry rule: 401/403/400 never retry; transient failures at
 * most twice after the first attempt (failureCount counts failed attempts).
 */
export function shouldRetryGet(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false;
  if (isAbortError(error)) return false;
  return !isNonRetryableStatus(statusOf(error));
}

export interface RequestOptions {
  signal?: AbortSignal | undefined;
  headers?: Record<string, string> | undefined;
}

export interface SendOptions extends RequestOptions {
  method?: string | undefined;
  body?: unknown;
}

function buildInit(
  method: string,
  options: SendOptions | undefined,
): RequestInit {
  const init: RequestInit = {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(options?.body !== undefined
        ? { "Content-Type": "application/json" }
        : {}),
      ...(options?.headers ?? {}),
    },
  };
  if (options?.signal !== undefined) {
    init.signal = options.signal;
  }
  if (options?.body !== undefined) {
    init.body = JSON.stringify(options.body);
  }
  return init;
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { message: text.slice(0, 500) };
  }
}

function errorMessage(status: number, body: unknown): string {
  if (typeof body === "object" && body !== null) {
    const record = body as Record<string, unknown>;
    const message = record.error ?? record.message;
    if (typeof message === "string" && message.trim() !== "") {
      return message;
    }
  }
  if (status === 401) return "Not authenticated.";
  if (status === 403) return "Forbidden.";
  if (status === 400) return "Bad request.";
  if (status === 404) return "Not found.";
  if (status === 409) return "Conflict.";
  if (status >= 500) return "Server error.";
  return `Request failed (${status}).`;
}

/** Core JSON request. Throws ApiError (or AbortError) on failure. */
export async function requestJson<T>(
  path: string,
  options?: SendOptions,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, buildInit(options?.method ?? "GET", options));
  } catch (error) {
    if (
      typeof error === "object" &&
      error !== null &&
      (error as { name?: unknown }).name === "AbortError"
    ) {
      const aborted = new ApiError("Request was cancelled.", 0);
      aborted.name = "AbortError";
      throw aborted;
    }
    throw new ApiError(
      error instanceof Error ? error.message : "Network request failed.",
      0,
    );
  }
  if (options?.signal?.aborted === true) {
    const aborted = new ApiError("Request was cancelled.", 0);
    aborted.name = "AbortError";
    throw aborted;
  }
  const body = await parseBody(response);
  if (!response.ok) {
    throw new ApiError(errorMessage(response.status, body), response.status, body);
  }
  return body as T;
}

/** Abortable same-origin JSON GET. */
export function apiGet<T>(path: string, options?: RequestOptions): Promise<T> {
  return requestJson<T>(path, { ...options, method: "GET" });
}

/** Same-origin JSON write (PATCH save path uses this via the controller). */
export function apiSend<T>(
  path: string,
  method: string,
  body: unknown,
  options?: RequestOptions,
): Promise<T> {
  return requestJson<T>(path, { ...options, method, body });
}

/**
 * GET with bounded retries for transient failures. Never retries
 * 400/401/403 or aborts. Delay grows linearly (150ms * attempt) and honors
 * the caller signal between attempts.
 */
export async function apiGetWithRetry<T>(
  path: string,
  options?: RequestOptions & { maxRetries?: number | undefined },
): Promise<T> {
  const maxRetries = options?.maxRetries ?? 2;
  let attempt = 0;
  for (;;) {
    try {
      return await apiGet<T>(path, options);
    } catch (error) {
      if (isAbortError(error)) throw error;
      if (!shouldRetryGet(attempt, error)) throw error;
      if (attempt >= maxRetries) throw error;
      attempt += 1;
      await delay(150 * attempt, options?.signal);
    }
  }
}

function delay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted === true) {
      const aborted = new ApiError("Request was cancelled.", 0);
      aborted.name = "AbortError";
      reject(aborted);
      return;
    }
    const timer = setTimeout(() => {
      cleanup();
      resolve();
    }, ms);
    function cleanup(): void {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
    }
    function onAbort(): void {
      cleanup();
      const aborted = new ApiError("Request was cancelled.", 0);
      aborted.name = "AbortError";
      reject(aborted);
    }
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/**
 * Attribution guard: returns true when an arrived response no longer
 * belongs to the current request and must be dropped (in addition to
 * aborting the request). Mirrors
 * `isStaleCorpusResponse` for the generic case.
 */
export function isStaleResponse(
  responseKey: string,
  currentKey: string,
): boolean {
  return responseKey !== currentKey;
}
