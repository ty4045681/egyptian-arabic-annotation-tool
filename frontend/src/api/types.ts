/**
 * Shared P1 API types (work packages A/B, src/api).
 *
 * Canonical request/response shapes for the preview data layer. Field names
 * are intentionally identical to the existing feature-local mirrors so the
 * E/F pages can adopt these without renames (additive only):
 * - features/admin/corpus/types.ts: CorpusTaskItem / CorpusListResponse /
 *   CorpusSubmitter / CorpusAssignment / FacetScene / FacetBatch /
 *   CorpusFacets / AdminSession.
 * - features/workspace/editor/editorStore.ts: SegmentValue (+ SegmentPatch).
 * - features/workspace/editor/sceneReview.ts: SceneReviewValue.
 * - features/workspace/persistence/types.ts: SaveRequestBody segments.
 * - features/auth/sessionErrors.ts: ApiError body shape.
 *
 * Do not rename fields here to "simplify" styling; task_status / status /
 * pool_state are not synonyms (see CorpusTaskItem docs).
 */

/** `GET /api/admin/session` response (admin_required). */
export interface AdminSession {
  authenticated: boolean;
  key_id?: string | null;
  csrf_token?: string | null;
  idle_expires_at?: string | null;
  absolute_expires_at?: string | null;
}

/** Scene option from `GET /api/admin/metadata/facets` (`scenes`). */
export interface FacetScene {
  code: string;
  label_en?: string | null;
  label_zh?: string | null;
  label?: string | null;
  sort_order?: number | null;
}

/** Batch option from `GET /api/admin/metadata/facets` (`batches`). */
export interface FacetBatch {
  batch_code: string;
  name?: string | null;
}

/**
 * `GET /api/admin/metadata/facets` response. Vocabulary only; counts live
 * on `admin_tasks` (`matched_count` / `matched_duration_seconds`).
 */
export interface AdminFacets {
  scenes: FacetScene[];
  batches: FacetBatch[];
  confidences: string[];
  review_statuses: string[];
  applied_filters?: Record<string, unknown> | undefined;
  as_of?: string | undefined;
}

/** Current submitter embedded on an `admin_tasks` row. */
export interface AdminSubmitter {
  id: string;
  username: string;
  status?: string | null;
}

/** Open assignment embedded on an `admin_tasks` row. */
export interface AdminAssignment {
  annotator_id: string;
  username: string;
  mode?: string | null;
  assigned_at?: string;
  last_activity_at?: string | null;
  working_version_id?: string;
}

/**
 * One row of `GET /api/admin/tasks` (`items`).
 *
 * Mirrors `CorpusTaskItem` exactly (annotation_repository.py `admin_tasks`).
 * `task_status`, `status`, and `pool_state` must never be conflated.
 */
export interface AdminTask {
  task_id: string;
  id: string;
  created_at?: string;
  updated_at?: string;
  filename: string;
  folder?: string | null;
  rel_path?: string | null;
  duration: number;
  task_status: string;
  status: string;
  pool_state?: string | null;
  eligible?: boolean;
  category?: string | null;
  current_version_id?: string | null;
  submitted_at?: string | null;
  current_submitter?: AdminSubmitter | null;
  submitted_by?: string | null;
  assignment?: AdminAssignment | null;
  source_scenes?: string[];
  source_confidence?: string | null;
  batch_codes?: string[];
  review_status?: string | null;
  prediction_label?: string | null;
  prediction_scene?: string | null;
  human_scenes?: string[];
}

/**
 * `GET /api/admin/tasks` response.
 * `matched_count` / `matched_duration_seconds` describe the whole filtered
 * set, not the loaded rows.
 */
export interface AdminTasksResponse {
  items: AdminTask[];
  next_cursor: string | null;
  matched_count: number;
  matched_duration_seconds: number;
  applied_filters?: Record<string, unknown> | undefined;
  filter_digest?: string | null | undefined;
}

/**
 * Transcription segment.
 *
 * Mirrors `SegmentValue` (editorStore) and `SaveSegmentPayload`
 * (persistence/types) plus the backend `_load_segments` row
 * (`id/start/end/duration/asr_text/text/exclude_from_training`).
 * Backend `extra` keys ride on the index signature; the six core keys
 * keep their names.
 */
export interface Segment {
  id: number | string;
  start: number;
  end: number;
  duration: number;
  text: string;
  asr_text?: string;
  exclude_from_training?: boolean;
  [field: string]: unknown;
}

/** Minimal scene-review value (mirrors `SceneReviewValue`). */
export type SceneReviewStatus = "pending" | "confirmed" | "mixed";

export interface SceneReview {
  status: SceneReviewStatus;
  scene_codes: string[];
  note: string;
  submitted?: boolean | undefined;
  [field: string]: unknown;
}

/**
 * `GET /api/assignment` active assignment payload.
 *
 * Mirrors `annotation_repository._row_to_assignment` plus metadata
 * attachments (`scene_review`, `sources`, `features`). Read-only here;
 * writes go through the persistence controller (`PATCH
 * /api/assignment/current` with `lease_token` / `expected_revision` /
 * `operation_id`).
 */
export interface Assignment {
  assigned: boolean;
  task_id: string;
  mode?: string | null;
  lease_token: string;
  assigned_at?: string | null;
  rel_path?: string | null;
  filename?: string | null;
  folder?: string | null;
  duration?: number | null;
  status?: string | null;
  version_id: string;
  revision: number;
  skip_reasons?: string[];
  segments: Segment[];
  waveform_b64?: string | null;
  resumed?: boolean;
  scene_review?: SceneReview | null;
  features?: Record<string, unknown> | null;
  [field: string]: unknown;
}

/** `GET /api/current-user` success payload. */
export interface CurrentUser {
  user: string;
  session?: {
    server_time?: string | null;
    idle_expires_at?: string | null;
    absolute_expires_at?: string | null;
    heartbeat_seconds?: number | null;
    idle_warning_seconds?: number | null;
    offline_draft_retention_days?: number | null;
  } | null;
}
