import { z } from "zod";

z.config({ jitless: true });

const text = z.string();
const nullableText = text.nullable();
const count = z.number();
export const sessionSchema = z.object({
  authenticated: z.literal(true),
  key_id: text,
  csrf_token: text.min(1),
});
export type AdminIdentity = z.infer<typeof sessionSchema>;
export const personSchema = z.object({
  id: text,
  username: text,
  status: text,
});
export const sceneSchema = z.object({
  code: text,
  label_en: text,
  label_zh: text.optional(),
});
export const facetsSchema = z.object({
  scenes: z.array(sceneSchema),
  batches: z.array(z.object({ batch_code: text, name: text.nullish() })),
  confidences: z.array(text),
  review_statuses: z.array(text),
});
export const pageSchema = <T extends z.ZodType>(item: T) =>
  z.object({
    items: z.array(item),
    next_cursor: nullableText,
    matched_count: count.optional(),
    matched_duration_seconds: count.optional(),
    state_counts: z.record(text, count).optional(),
  });
const currentSchema = z.object({
  annotated_count: count,
  skipped_count: count,
  duration_seconds: count,
});
const historySchema = z.object({
  completed_count: count,
  revoked_count: count,
  last_completed_at: nullableText,
});
export const assignmentSchema = z.object({
  task_id: text.optional(),
  annotator_id: text.optional(),
  username: text.optional(),
  filename: text.optional(),
  assigned_at: text,
  last_activity_at: nullableText,
});
export const annotatorSchema = personSchema.extend({
  current: currentSchema,
  history: historySchema,
  assignment: assignmentSchema.nullable(),
  online: z.boolean(),
  created_at: text,
  deactivated_at: nullableText,
  deactivated_reason: nullableText,
});
export type Annotator = z.infer<typeof annotatorSchema>;
export const scopeSchema = z.object({
  mode: z.enum(["all", "restricted", "none"]),
  scene_codes: z.array(text),
  allow_unknown: z.boolean(),
  revision: count,
});
export const annotatorDetailSchema = personSchema.extend({
  current: currentSchema.extend({
    annotated_duration_seconds: count,
    trainable_duration_seconds: count,
  }),
  history: historySchema.extend({
    active_days: count,
    revision_count: count,
    abandoned_count: count,
  }),
  activity: z.object({
    online: z.boolean(),
    last_activity_at: nullableText,
    last_seen_at: nullableText,
  }),
  efficiency: z.object({
    turnaround_average_seconds: count.nullable(),
    turnaround_median_seconds: count.nullable(),
  }),
  quality: z.object({
    excluded_count: count,
    excluded_ratio: count,
    segment_count: count,
  }),
  assignment: assignmentSchema.nullable(),
  scene_scope: scopeSchema,
});
export type AnnotatorDetail = z.infer<typeof annotatorDetailSchema>;
export const annotationSchema = z.object({
  task_id: text,
  version_id: text,
  filename: text,
  rel_path: text,
  duration: count,
  status: text,
  submitted_at: nullableText,
  submitted_by: nullableText,
  annotator: personSchema,
  is_current: z.boolean(),
  can_revoke: z.boolean(),
  lifecycle: text,
  source_scenes: z.array(text),
  source_confidence: nullableText,
  review_status: nullableText,
});
export const crossSummarySchema = z.object({
  pending_review_count: count,
  in_progress_count: count,
  passed_count: count,
  adjudicated_count: count,
  cancelled_count: count,
  invalidated_count: count,
  blocked_audio_seconds: count,
  oldest_pending_created_at: nullableText,
});
const distributionSchema = z.object({
  task_count: count,
  duration_seconds: count,
});
export const overviewSchema = z.object({
  totals: z.object({
    total_audio_count: count,
    total_audio_duration_seconds: count,
    annotated_count: count,
    annotated_duration_seconds: count,
    pending_count: count,
    pending_duration_seconds: count,
    skipped_count: count,
    skipped_duration_seconds: count,
    cross_check_submitted_count: count,
    cross_check_submitted_audio_seconds: count,
  }),
  pending: z.object({
    available_count: count,
    assigned_count: count,
    reserved_count: count,
    ineligible_count: count,
    oldest_created_at: nullableText,
  }),
  segments: z.object({
    total_count: count,
    trainable_count: count,
    trainable_duration_seconds: count,
  }),
  activity: z.object({ active_annotators: count, daily_throughput_7d: count }),
  queue_health: z.object({ stale_assignments: count }),
  cross_check: crossSummarySchema,
  categories: z.array(z.object({ category: text, count })),
  skip_reasons: z.array(z.object({ reason: text, count })),
  source_scenes: z.array(
    distributionSchema.extend({ scene_code: text, label: text }),
  ),
  confidence_buckets: z.array(distributionSchema.extend({ confidence: text })),
  source_batches: z.array(distributionSchema.extend({ batch_code: text })),
  review_statuses: z.array(distributionSchema.extend({ status: text })),
  updated_at: text,
});
export type Overview = z.infer<typeof overviewSchema>;
export const timeseriesSchema = z.object({
  bucket: text,
  items: z.array(
    z.object({
      period: text,
      annotated: count,
      skipped: count,
      revoked: count,
    }),
  ),
});
export type Timeseries = z.infer<typeof timeseriesSchema>;
export const segmentSchema = z.object({
  id: z.union([text, count]),
  start: count,
  end: count,
  duration: count,
  text,
  asr_text: text.optional(),
  exclude_from_training: z.boolean().default(false),
});
export type Segment = z.infer<typeof segmentSchema>;
export const reviewSchema = z.object({
  id: nullableText.optional(),
  status: text,
  scene_codes: z.array(text),
  note: text,
});
export type SceneReview = z.infer<typeof reviewSchema>;
export const sourceSchema = z.object({
  id: text,
  scene_code: text.nullish(),
  scene_label: text.nullish(),
  confidence: text,
  confidence_basis: text.nullish(),
  source_url: text.nullish(),
  batch_code: text,
  is_current: z.boolean().optional(),
});
export const taskDetailSchema = z.object({
  task_id: text,
  filename: text,
  rel_path: text,
  duration: count,
  status: text,
  current_version_id: nullableText,
  display_version_id: nullableText,
  baseline_quality: nullableText,
  assignment: assignmentSchema.nullable(),
  segments: z.array(segmentSchema),
  metadata: z
    .object({
      headline: text,
      notice: text,
      scene_review: reviewSchema.nullable(),
      features: z.object({ scene_review_write: z.boolean() }),
    })
    .nullish(),
  source_history: z.array(sourceSchema).default([]),
  versions: z.array(
    z.object({
      id: text,
      version_no: count,
      lifecycle: text,
      is_current: z.boolean(),
      submitted_at: nullableText,
      revoked_by_admin_action_id: nullableText,
      revoked_reason: nullableText,
      submitter: personSchema.omit({ status: true }).nullable(),
    }),
  ),
});
export type TaskDetail = z.infer<typeof taskDetailSchema>;
export const qualitySchema = z.object({
  stats: z.object({
    excluded_segment_count: count,
    excluded_segment_rate: count,
    revoked_count: count,
    stale_assignments: count,
    unusually_fast: count,
  }),
  items: z.array(
    z.object({
      task_id: text,
      filename: text,
      username: text.nullish(),
      annotator_id: text.nullish(),
      type: text,
      created_at: text.nullish(),
      elapsed_seconds: count.nullish(),
      last_activity_at: text.nullish(),
    }),
  ),
});
export const auditSchema = z.object({
  id: text,
  action_type: text,
  created_at: text,
  key_id: nullableText,
  reason: text,
  status: text,
  item_count: count,
});
export const crossStateSchema = z.enum([
  "awaiting_review",
  "in_progress",
  "passed",
  "adjudicated",
  "cancelled",
  "invalidated",
]);
export const crossRoundSchema = z.object({
  round_id: text,
  task_id: text,
  filename: text.nullish(),
  source_scene: nullableText.optional(),
  duration_seconds: count,
  original_annotator_id: nullableText,
  secondary_annotator_id: nullableText,
  state: crossStateSchema,
  word_difference_rate: count.nullable(),
  reason_codes: z.array(text),
  created_at: text,
  submitted_at: nullableText,
  training_export_blocked: z.boolean(),
});
const diffLocationSchema = z.object({
  segment_id: z.union([text, count]),
  text_start: count,
  text_end: count,
  start_s: count,
  end_s: count,
});
export const crossDetailSchema = crossRoundSchema.extend({
  filename: text,
  revision: count,
  original_version_id: text,
  secondary_version_id: nullableText,
  current_published_version_id: nullableText,
  final_version_id: nullableText.optional(),
  original_segments: z.array(segmentSchema),
  secondary_segments: z.array(segmentSchema),
  original_review: reviewSchema.nullable(),
  secondary_review: reviewSchema.nullable(),
  original_target_status: text,
  secondary_target_status: nullableText,
  original_skip_reasons: z.array(text),
  secondary_skip_reasons: z.array(text),
  original_word_count: count.nullable(),
  secondary_word_count: count.nullable(),
  substitutions: count.nullable(),
  deletions: count.nullable(),
  insertions: count.nullable(),
  diff_ops: z
    .array(
      z.object({
        op: text,
        original: diffLocationSchema.nullable(),
        secondary: diffLocationSchema.nullable(),
      }),
    )
    .nullable(),
  comparison_unavailable: z.boolean(),
  comparison_unavailable_reason: nullableText,
  audio_url: text,
  decision: nullableText,
  decision_reason: nullableText,
  termination_reason: nullableText,
});
export type CrossDetail = z.infer<typeof crossDetailSchema>;
export const settingsSchema = z.object({
  enabled: z.boolean(),
  sampling_rate_bps: count,
  revision: count,
  comparison_version: text,
  word_difference_threshold_bps: count,
});
export const writeResultSchema = z.union([
  z.object({ success: z.literal(true) }).passthrough(),
  settingsSchema,
]);
export const revokePreviewSchema = z.object({
  items: z.array(
    z.object({
      task_id: text,
      filename: nullableText,
      revokeable: z.boolean(),
      conflict: nullableText,
      will_invalidate_cross_check: z.boolean(),
    }),
  ),
  summary: z.object({
    requested: count,
    revokeable: count,
    conflicts: count,
    duration_seconds: count,
    open_cross_check_rounds: count,
  }),
});
export const deactivatePreviewSchema = z.object({
  summary: z.object({
    published_to_revoke: count,
    published_duration_seconds: count,
    assignments_to_release: count,
    sessions_to_revoke: count,
    cross_check_in_progress_to_cancel: count,
    cross_check_awaiting_review_kept: count,
  }),
});

export interface RevokeTarget {
  task_id: string;
  expected_version_id: string;
  filename: string;
  annotator_id: string | null;
}
export type AdminAction =
  | { kind: "revoke"; items: RevokeTarget[] }
  | { kind: "deactivate"; annotator: { id: string; username: string } }
  | { kind: "release"; taskId: string; filename: string }
  | { kind: "restore"; taskId: string; actionId: string; filename: string };

export const filterCountsSchema = z.object({
  status: z.record(text, count),
  confidence: z.record(text, count),
  scenes: z.record(text, count),
});
