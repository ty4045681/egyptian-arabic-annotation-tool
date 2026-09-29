/**
 * Bounded retry backoff (work package F, workspace/persistence).
 *
 * Offline and transient failures retry with a capped exponential delay and
 * then park in "waiting for connection" instead of busy-looping. Auth and
 * conflict outcomes never retry: 401 / deactivated-403 stop immediately,
 * 409 freezes for an explicit user decision.
 */

export const BACKOFF_BASE_MS = 1000;
export const BACKOFF_CAP_MS = 8000;
export const BACKOFF_MAX_ATTEMPTS = 8;

export function backoffDelayMs(attempt: number): number {
  const n = Math.max(0, Math.floor(attempt));
  return Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** n);
}

export function shouldRetry(status: number, code?: string): boolean {
  if (status === 0) return true;
  if (status === 401) return false;
  if (status === 403 && code === "account_deactivated") return false;
  if (status === 409) return false;
  if (status >= 500 && status <= 599) return true;
  if (status === 408 || status === 429) return true;
  return false;
}

export interface BackoffState {
  attempts: number;
  nextDelayMs: number | null;
  exhausted: boolean;
}

export function nextBackoff(attempts: number): BackoffState {
  if (attempts >= BACKOFF_MAX_ATTEMPTS) {
    return { attempts, nextDelayMs: null, exhausted: true };
  }
  return { attempts: attempts + 1, nextDelayMs: backoffDelayMs(attempts), exhausted: false };
}
