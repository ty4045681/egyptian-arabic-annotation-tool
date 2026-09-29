/**
 * Persistence protocol types (work package F, workspace/persistence).
 *
 * PATCH /api/assignment/current always carries lease_token,
 * expected_revision, operation_id and the actual segments payload;
 * scene_review is included only when a review change is confirmed and the
 * scene_review_write flag allows it.
 */

import type { SceneReviewValue } from "../editor/sceneReview";
import type { SegmentValue } from "../editor/editorStore";

export interface SaveSegmentPayload {
  id: number | string;
  start: number;
  end: number;
  duration: number;
  text: string;
  exclude_from_training: boolean;
  [field: string]: unknown;
}

export interface SaveRequestBody {
  lease_token: string;
  expected_revision: number;
  operation_id: string;
  segments: SaveSegmentPayload[];
  scene_review?: SceneReviewValue;
}

export interface SaveResult {
  revision: number;
  scene_review?: SceneReviewValue | null;
}

export type SaveStatus =
  | "idle"
  | "local"
  | "syncing"
  | "synced"
  | "offline"
  | "conflict"
  | "frozen"
  | "storage-error"
  | "terminal-unsupported";

export type FreezeReason =
  | "unauthorized"
  | "account-deactivated"
  | "revision-conflict"
  | "assignment-invalid"
  | "terminal-outbox"
  | "manual";

export interface OutboxRecord {
  operation_id: string;
  username: string;
  task_id: string;
  route: string;
  method: string;
  body: SaveRequestBody;
  created_at: string;
  attempts: number;
}

export interface PendingTimeEditRecord {
  segment_id: number | string;
  field: "start" | "end";
  raw: string;
}

export interface WorkingDraftRecord {
  schema_version: number;
  updated_at: string;
  status: "dirty" | "clean";
  username: string;
  task_id: string;
  version_id: string;
  lease_token: string;
  server_revision: number;
  segments: SegmentValue[];
  scene_review: SceneReviewValue | null;
  dirty_segment_ids: Array<number | string>;
  review_dirty: boolean;
  mode?: string;
  round_id?: string;
  /**
   * Uncommitted time-field raw text (AA03). Optional and backward
   * compatible: absent means no pending edits (old drafts read as empty).
   * Never store raw time in the numeric segment fields.
   */
  pending_time_edits?: PendingTimeEditRecord[];
}

export const SAVE_ROUTE = "/api/assignment/current";
export const SAVE_METHOD = "PATCH";

/** Routes the P1 controller never replays: complete/abandon need P4. */
export function isTerminalOutboxRoute(route: string | null | undefined): boolean {
  if (!route) return false;
  return /\/complete$|\/abandon$/.test(route);
}

export function toSaveSegment(segment: SegmentValue): SaveSegmentPayload {
  return {
    id: segment.id,
    start: Number(segment.start),
    end: Number(segment.end),
    duration: Number(segment.duration),
    text: String(segment.text ?? ""),
    exclude_from_training: Boolean(segment.exclude_from_training),
  };
}

export function deepFreeze<T>(value: T): T {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    if (Array.isArray(value)) {
      value.forEach(deepFreeze);
    } else {
      for (const key of Object.keys(value as Record<string, unknown>)) {
        deepFreeze((value as Record<string, unknown>)[key]);
      }
    }
    Object.freeze(value);
  }
  return value;
}
