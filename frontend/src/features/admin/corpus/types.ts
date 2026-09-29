import { z } from "zod";
import { facetsSchema, filterCountsSchema, sessionSchema } from "../schemas";

export type AdminSession = z.infer<typeof sessionSchema>;
export type CorpusFacets = z.infer<typeof facetsSchema>;

export const corpusTaskSchema = z.object({
  task_id: z.string(),
  id: z.string(),
  created_at: z.string().optional(),
  updated_at: z.string().optional(),
  filename: z.string(),
  folder: z.string().nullish(),
  rel_path: z.string().nullish(),
  duration: z.number().nonnegative(),
  task_status: z.string(),
  status: z.string(),
  pool_state: z.string().nullish(),
  eligible: z.boolean().optional(),
  category: z.string().nullish(),
  current_version_id: z.string().nullish(),
  submitted_at: z.string().nullish(),
  current_submitter: z
    .object({
      id: z.string(),
      username: z.string(),
      status: z.string().nullish(),
    })
    .nullish(),
  submitted_by: z.string().nullish(),
  assignment: z
    .object({
      annotator_id: z.string(),
      username: z.string(),
      mode: z.string().nullish(),
      assigned_at: z.string().optional(),
      last_activity_at: z.string().nullish(),
      working_version_id: z.string().optional(),
    })
    .nullish(),
  source_scenes: z.array(z.string()).optional(),
  source_confidence: z.string().nullish(),
  batch_codes: z.array(z.string()).optional(),
  review_status: z.string().nullish(),
  prediction_label: z.string().nullish(),
  prediction_scene: z.string().nullish(),
  human_scenes: z.array(z.string()).optional(),
});

export type CorpusTaskItem = z.infer<typeof corpusTaskSchema>;

export const corpusListSchema = z.object({
  items: z.array(corpusTaskSchema),
  next_cursor: z.string().nullable(),
  matched_count: z.number().int().nonnegative(),
  matched_duration_seconds: z.number().nonnegative(),
  filter_counts: filterCountsSchema.nullish(),
  applied_filters: z.record(z.string(), z.unknown()).optional(),
  filter_digest: z.string().nullish(),
});

export type CorpusListResponse = z.infer<typeof corpusListSchema>;
