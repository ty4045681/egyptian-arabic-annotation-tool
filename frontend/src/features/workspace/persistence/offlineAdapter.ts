/**
 * IndexedDB adapter (work package F, workspace/persistence).
 *
 * P1 reuses the legacy database `annotation-offline-v1` (version 1) with the
 * `working_drafts` / `outbox` stores through the existing
 * static/offline-drafts.js global — no new schema, no clearing of other
 * users' records. The adapter adds TypeScript types plus explicit storage
 * health reporting (unavailable / quota / corrupt are independent states
 * that never clear unconfirmed outbox entries).
 */

import type { OutboxRecord, SaveRequestBody, WorkingDraftRecord } from "./types";

export type StorageHealth = "ok" | "unavailable" | "quota-exceeded" | "corrupt";

/**
 * Distinguishable read outcome (W08): a missing record and a failed read are
 * NOT the same thing. Callers must never fold an error into "no draft" and
 * then overwrite unknown data.
 */
export type ReadResult<T> =
  | { status: "ok"; value: T }
  | { status: "missing" }
  | { status: "error"; health: StorageHealth; error: string };

export interface OfflineStore {
  draftKey: (username: string, taskId: string) => string;
  saveWorkingDraft: (draft: WorkingDraftRecord) => Promise<WorkingDraftRecord>;
  getWorkingDraft: (username: string, taskId: string) => Promise<WorkingDraftRecord | null>;
  readWorkingDraft: (username: string, taskId: string) => Promise<ReadResult<WorkingDraftRecord>>;
  listWorkingDrafts: (username: string) => Promise<WorkingDraftRecord[]>;
  deleteWorkingDraft: (username: string, taskId: string) => Promise<void>;
  putOutbox: (item: OutboxRecord) => Promise<OutboxRecord>;
  getOutboxItem: (operationId: string) => Promise<OutboxRecord | null>;
  listOutbox: (username: string) => Promise<OutboxRecord[]>;
  readOutbox: (username: string) => Promise<ReadResult<OutboxRecord[]>>;
  bumpOutboxAttempt: (operationId: string) => Promise<void>;
  confirmOutbox: (
    operationId: string,
    extras: { server_revision?: number; clear_dirty?: boolean; delete_draft?: boolean },
  ) => Promise<void>;
  deleteTaskData: (username: string, taskId: string) => Promise<void>;
  purgeExpired: (retentionDays: number, username: string | undefined) => Promise<void>;
  exportText: (draft: WorkingDraftRecord | null | undefined) => string;
}

export const DB_NAME = "annotation-offline-v1";
export const DRAFT_KEY_SEPARATOR = ":";

/** Same key derivation as the legacy draftKey(): username + ":" + task_id. */
export function draftKey(username: string, taskId: string): string {
  return `${String(username)}${DRAFT_KEY_SEPARATOR}${String(taskId)}`;
}

function legacyGlobal(): Record<string, (...args: never[]) => Promise<unknown>> | null {
  const scope = globalThis as unknown as { AnnotationOffline?: unknown };
  if (!scope.AnnotationOffline || typeof scope.AnnotationOffline !== "object") return null;
  return scope.AnnotationOffline as Record<string, (...args: never[]) => Promise<unknown>>;
}

export function legacyStoreAvailable(): boolean {
  return legacyGlobal() !== null;
}

function classifyStorageError(error: unknown): StorageHealth {
  const name = (error as { name?: unknown })?.name;
  if (name === "QuotaExceededError" || name === "NS_ERROR_DOM_QUOTA_REACHED") return "quota-exceeded";
  if (error instanceof SyntaxError) return "corrupt";
  const message = String((error as { message?: unknown })?.message ?? error ?? "");
  if (/quota/i.test(message)) return "quota-exceeded";
  if (/corrupt|invalidstate|unknownerror/i.test(message)) return "corrupt";
  return "unavailable";
}

/**
 * Adapter that delegates every call to the legacy global, preserving its
 * exact semantics (draftKey, immutable putOutbox, transactional
 * confirmOutbox, exportText stripping mode/round_id).
 */
export class LegacyOfflineAdapter implements OfflineStore {
  private health: StorageHealth = "ok";
  private lastError: string | null = null;

  get storageHealth(): StorageHealth {
    return this.health;
  }

  get storageError(): string | null {
    return this.lastError;
  }

  draftKey(username: string, taskId: string): string {
    return draftKey(username, taskId);
  }

  private api(): Record<string, (...args: never[]) => Promise<unknown>> {
    const api = legacyGlobal();
    if (!api) {
      this.health = "unavailable";
      this.lastError = "AnnotationOffline is not loaded";
      throw new Error("AnnotationOffline is not loaded");
    }
    return api;
  }

  private async call<T>(method: string, ...args: unknown[]): Promise<T> {
    const api = this.api();
    try {
      const result = (await (api[method] as (...callArgs: unknown[]) => Promise<T>)(...args)) as T;
      this.health = "ok";
      this.lastError = null;
      return result;
    } catch (error) {
      this.health = classifyStorageError(error);
      this.lastError = error instanceof Error ? error.message : String(error);
      throw error;
    }
  }

  saveWorkingDraft(draft: WorkingDraftRecord): Promise<WorkingDraftRecord> {
    return this.call<WorkingDraftRecord>("saveWorkingDraft", draft);
  }

  getWorkingDraft(username: string, taskId: string): Promise<WorkingDraftRecord | null> {
    return this.call<WorkingDraftRecord | null>("getWorkingDraft", username, taskId);
  }

  /**
   * Distinguishable draft read (W08). A missing key is `missing`; a thrown
   * read (IDB error) is `error` with health, never silently `null`.
   */
  async readWorkingDraft(
    username: string,
    taskId: string,
  ): Promise<ReadResult<WorkingDraftRecord>> {
    try {
      const value = await this.call<WorkingDraftRecord | null>(
        "getWorkingDraft",
        username,
        taskId,
      );
      return value ? { status: "ok", value } : { status: "missing" };
    } catch (error) {
      return {
        status: "error",
        health: this.health,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  listWorkingDrafts(username: string): Promise<WorkingDraftRecord[]> {
    return this.call<WorkingDraftRecord[]>("listWorkingDrafts", username);
  }

  deleteWorkingDraft(username: string, taskId: string): Promise<void> {
    return this.call<void>("deleteWorkingDraft", username, taskId);
  }

  putOutbox(item: OutboxRecord): Promise<OutboxRecord> {
    return this.call<OutboxRecord>("putOutbox", item);
  }

  getOutboxItem(operationId: string): Promise<OutboxRecord | null> {
    return this.call<OutboxRecord | null>("getOutboxItem", operationId);
  }

  listOutbox(username: string): Promise<OutboxRecord[]> {
    return this.call<OutboxRecord[]>("listOutbox", username);
  }

  /** Distinguishable outbox read (W08/V04): error is not an empty list. */
  async readOutbox(username: string): Promise<ReadResult<OutboxRecord[]>> {
    try {
      const value = await this.call<OutboxRecord[]>("listOutbox", username);
      return { status: "ok", value };
    } catch (error) {
      return {
        status: "error",
        health: this.health,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }

  bumpOutboxAttempt(operationId: string): Promise<void> {
    return this.call<void>("bumpOutboxAttempt", operationId);
  }

  confirmOutbox(
    operationId: string,
    extras: { server_revision?: number; clear_dirty?: boolean; delete_draft?: boolean },
  ): Promise<void> {
    return this.call<void>("confirmOutbox", operationId, extras);
  }

  deleteTaskData(username: string, taskId: string): Promise<void> {
    return this.call<void>("deleteTaskData", username, taskId);
  }

  /** Remove drafts/outbox older than the retention window (W04). */
  purgeExpired(retentionDays: number, username: string | undefined): Promise<void> {
    return this.call<void>("purgeExpired", retentionDays, username);
  }

  exportText(draft: WorkingDraftRecord | null | undefined): string {
    const api = legacyGlobal();
    if (!api) {
      const copy = { ...(draft ?? {}) } as Record<string, unknown>;
      delete copy.mode;
      delete copy.round_id;
      return JSON.stringify(copy, null, 2);
    }
    try {
      return String(
        (api.exportText as unknown as (input: unknown) => string)(draft),
      );
    } catch (error) {
      this.health = classifyStorageError(error);
      this.lastError = error instanceof Error ? error.message : String(error);
      throw error;
    }
  }
}

/**
 * Validate a stored draft before trusting it: corrupt records surface a
 * storage-error status and are never applied, replayed, or deleted.
 */
export function validateStoredDraft(value: unknown): {
  ok: boolean;
  draft?: WorkingDraftRecord;
  reason?: string;
} {
  if (!value || typeof value !== "object") return { ok: false, reason: "missing draft" };
  const draft = value as Record<string, unknown>;
  if (typeof draft.username !== "string" || typeof draft.task_id !== "string") {
    return { ok: false, reason: "missing identity" };
  }
  if (!Array.isArray(draft.segments)) return { ok: false, reason: "segments are not an array" };
  if (typeof draft.server_revision !== "number" || typeof draft.lease_token !== "string") {
    return { ok: false, reason: "missing revision context" };
  }
  // AB03: pending_time_edits is external data. Validate its shape and ensure
  // every entry resolves to a segment in THIS draft; an unexplained entry must
  // not be silently dropped later.
  if (draft.pending_time_edits !== undefined) {
    if (!Array.isArray(draft.pending_time_edits)) {
      return { ok: false, reason: "pending_time_edits is not an array" };
    }
    const segmentIds = new Set(
      (draft.segments as Array<{ id?: unknown }>).map((seg) => String(seg?.id)),
    );
    const seen = new Set<string>();
    for (const raw of draft.pending_time_edits) {
      if (!raw || typeof raw !== "object") {
        return { ok: false, reason: "pending_time_edits entry is not an object" };
      }
      const entry = raw as Record<string, unknown>;
      if (entry.field !== "start" && entry.field !== "end") {
        return { ok: false, reason: "pending_time_edits entry has an unknown field" };
      }
      if (typeof entry.raw !== "string") {
        return { ok: false, reason: "pending_time_edits entry raw is not a string" };
      }
      const segmentId = entry.segment_id;
      if (typeof segmentId !== "string" && typeof segmentId !== "number") {
        return { ok: false, reason: "pending_time_edits entry has no segment id" };
      }
      const key = `${String(segmentId)}:${entry.field}`;
      if (seen.has(key)) {
        return { ok: false, reason: "pending_time_edits has a duplicate entry" };
      }
      seen.add(key);
      if (!segmentIds.has(String(segmentId))) {
        return { ok: false, reason: "pending_time_edits references an unknown segment" };
      }
    }
  }
  return { ok: true, draft: draft as unknown as WorkingDraftRecord };
}

export function validateStoredOutbox(value: unknown): {
  ok: boolean;
  item?: OutboxRecord;
  reason?: string;
} {
  if (!value || typeof value !== "object") return { ok: false, reason: "missing outbox item" };
  const item = value as Record<string, unknown>;
  if (typeof item.operation_id !== "string" || !item.operation_id) {
    return { ok: false, reason: "missing operation_id" };
  }
  if (typeof item.route !== "string" || typeof item.method !== "string") {
    return { ok: false, reason: "missing route/method" };
  }
  const body = item.body as Record<string, unknown> | undefined;
  if (!body || typeof body !== "object") return { ok: false, reason: "missing body" };
  if (typeof body.operation_id !== "string" || body.operation_id !== item.operation_id) {
    return { ok: false, reason: "operation_id/body mismatch" };
  }
  if (typeof body.lease_token !== "string" || typeof body.expected_revision !== "number") {
    return { ok: false, reason: "missing save context" };
  }
  if (!Array.isArray(body.segments)) return { ok: false, reason: "segments are not an array" };
  return { ok: true, item: item as unknown as OutboxRecord };
}

/**
 * Classify one stored outbox envelope at the IndexedDB boundary (Y05). The
 * TypeScript `OutboxRecord[]` annotation is not runtime truth, so callers
 * must inspect `unknown`.
 *
 * - `save`: exactly `PATCH /api/assignment/current` with a valid body.
 * - `terminal`: a known `/complete` or `/abandon` route (hand to legacy).
 * - `unknown`: any other method/route envelope; retain, never reinterpret.
 * - `corrupt`: missing/invalid fields.
 */
const SAVE_TERMINAL_ROUTES = ["/complete", "/abandon"];

export function classifyStoredOutbox(
  value: unknown,
): "save" | "terminal" | "unknown" | "corrupt" {
  if (!value || typeof value !== "object") return "corrupt";
  const item = value as Record<string, unknown>;
  if (typeof item.route !== "string" || typeof item.method !== "string") return "corrupt";
  const route = item.route;
  const method = item.method.toUpperCase();
  if (SAVE_TERMINAL_ROUTES.some((suffix) => route.endsWith(suffix))) return "terminal";
  const isSaveEnvelope =
    method === "PATCH" && route === "/api/assignment/current";
  if (!isSaveEnvelope) return "unknown";
  return validateStoredOutbox(value).ok ? "save" : "corrupt";
}

/** Build the frozen outbox record for one immutable save operation. */
export function toOutboxRecord(
  username: string,
  taskId: string,
  body: SaveRequestBody,
  createdAt: string = new Date().toISOString(),
): OutboxRecord {
  return {
    operation_id: body.operation_id,
    username,
    task_id: taskId,
    route: "/api/assignment/current",
    method: "PATCH",
    body,
    created_at: createdAt,
    attempts: 0,
  };
}
