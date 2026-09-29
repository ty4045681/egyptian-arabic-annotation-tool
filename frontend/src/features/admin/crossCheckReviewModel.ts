import { z } from "zod";
import {
  segmentSchema,
  reviewSchema,
  type CrossDetail,
  type Segment,
} from "./schemas";
import type { Decision, Manuscript } from "./crossCheckModel";

type Manuscripts = Pick<
  CrossDetail,
  | `${Manuscript}_segments`
  | `${Manuscript}_target_status`
  | `${Manuscript}_skip_reasons`
>;

export const reviewDraftSchema = z.object({
  editor: z
    .object({
      base: z.enum(["original", "secondary"]),
      target: z.enum(["annotated", "skipped"]),
      skipReasons: z.array(z.string()),
      segments: z.array(segmentSchema),
    })
    .nullable(),
  reason: z.string(),
  override: z.boolean(),
  review: reviewSchema,
  picks: z.record(z.string(), z.enum(["original", "secondary", "edited"])),
});
export type ReviewDraft = z.infer<typeof reviewDraftSchema>;
export type ReviewRow = {
  id: string;
  original: Segment | null;
  secondary: Segment | null;
  start: number;
  end: number;
  differences: number[];
  different: boolean;
  compatible: boolean;
};

export function emptyReviewDraft(): ReviewDraft {
  return {
    editor: null,
    reason: "",
    override: false,
    review: { status: "pending", scene_codes: [], note: "" },
    picks: {},
  };
}

export function reviewRows(
  data: Pick<
    CrossDetail,
    | "original_segments"
    | "secondary_segments"
    | "diff_ops"
    | "comparison_unavailable"
  >,
): ReviewRow[] {
  const originals = new Map(
    data.original_segments.map((segment) => [String(segment.id), segment]),
  );
  const secondaries = new Map(
    data.secondary_segments.map((segment) => [String(segment.id), segment]),
  );
  const differences = new Map<string, number[]>();
  (data.diff_ops ?? []).forEach((op, index) => {
    if (op.op === "match") return;
    const ids = new Set(
      [op.original?.segment_id, op.secondary?.segment_id]
        .filter((id) => id !== undefined)
        .map(String),
    );
    for (const id of ids)
      differences.set(id, [...(differences.get(id) ?? []), index]);
  });
  return [...new Set([...originals.keys(), ...secondaries.keys()])]
    .map((id) => {
      const original = originals.get(id) ?? null;
      const secondary = secondaries.get(id) ?? null;
      const ops = differences.get(id) ?? [];
      const compatible =
        original !== null &&
        secondary !== null &&
        original.start === secondary.start &&
        original.end === secondary.end;
      return {
        id,
        original,
        secondary,
        start: Math.min(
          original?.start ?? Infinity,
          secondary?.start ?? Infinity,
        ),
        end: Math.max(original?.end ?? 0, secondary?.end ?? 0),
        differences: ops,
        compatible,
        different:
          !compatible ||
          ops.length > 0 ||
          original?.exclude_from_training !==
            secondary?.exclude_from_training ||
          (data.comparison_unavailable && original?.text !== secondary?.text),
      };
    })
    .sort(
      (a, b) =>
        a.start - b.start ||
        a.id.localeCompare(b.id, undefined, { numeric: true }),
    );
}

export function copyManuscript(
  data: Manuscripts,
  base: Manuscript,
): NonNullable<ReviewDraft["editor"]> {
  return {
    base,
    target:
      data[`${base}_target_status`] === "skipped" ? "skipped" : "annotated",
    skipReasons: [...data[`${base}_skip_reasons`]],
    segments: data[`${base}_segments`].map((segment) => ({ ...segment })),
  };
}

export function pickReviewSegment(
  data: Manuscripts,
  draft: ReviewDraft,
  row: ReviewRow,
  choice: Decision,
): ReviewDraft {
  const editor = draft.editor ?? copyManuscript(data, "original");
  const source = choice === "edited" ? null : row[choice];
  const base = editor.segments.find((segment) => String(segment.id) === row.id);
  if (!base || (choice !== "edited" && (!source || !row.compatible)))
    return draft;
  return {
    ...draft,
    editor: {
      ...editor,
      segments: editor.segments.map((segment) =>
        String(segment.id) !== row.id || !source
          ? segment
          : {
              ...segment,
              text: source.text,
              exclude_from_training: source.exclude_from_training,
            },
      ),
    },
    picks: { ...draft.picks, [row.id]: choice },
  };
}

export function unresolvedRows(
  rows: ReviewRow[],
  draft: ReviewDraft,
): ReviewRow[] {
  return rows.filter((row) => row.different && !draft.picks[row.id]);
}

export function reviewDecisionBody(
  data: Pick<
    CrossDetail,
    "revision" | "original_version_id" | "secondary_version_id"
  >,
  draft: ReviewDraft,
) {
  const editor = draft.editor;
  return {
    expected_revision: data.revision,
    expected_original_version_id: data.original_version_id,
    expected_secondary_version_id: data.secondary_version_id,
    decision: editor ? "edited" : null,
    reason: draft.reason.trim(),
    ...(editor
      ? {
          base: editor.base,
          target_status: editor.target,
          skip_reasons: editor.target === "skipped" ? editor.skipReasons : [],
          segments: editor.segments.map(
            ({ id, start, end, duration, text, exclude_from_training }) => ({
              id,
              start,
              end,
              duration,
              text,
              exclude_from_training,
            }),
          ),
          ...(draft.override
            ? {
                scene_review: {
                  status: draft.review.status,
                  scene_codes: draft.review.scene_codes,
                  note: draft.review.note,
                },
              }
            : {}),
        }
      : {}),
  };
}

const savedReviewSchema = z.object({
  revision: z.number(),
  originalVersion: z.string(),
  secondaryVersion: z.string().nullable(),
  draft: reviewDraftSchema,
});
const draftPrefix = "admin-crosscheck-review:";
const draftKey = (keyId: string, roundId: string) =>
  `${draftPrefix}${keyId}:${roundId}`;
export function loadReviewDraft(keyId: string, roundId: string) {
  try {
    const raw = sessionStorage.getItem(draftKey(keyId, roundId));
    return raw ? savedReviewSchema.parse(JSON.parse(raw)) : null;
  } catch {
    return null;
  }
}
export function saveReviewDraft(
  keyId: string,
  data: CrossDetail,
  draft: ReviewDraft,
): void {
  sessionStorage.setItem(
    draftKey(keyId, data.round_id),
    JSON.stringify({
      revision: data.revision,
      originalVersion: data.original_version_id,
      secondaryVersion: data.secondary_version_id,
      draft,
    }),
  );
}
export function removeReviewDraft(keyId: string, roundId: string): void {
  try {
    sessionStorage.removeItem(draftKey(keyId, roundId));
  } catch {
    /* Storage can be disabled by the browser. */
  }
}
export function clearReviewDrafts(): void {
  try {
    for (const key of Object.keys(sessionStorage))
      if (key.startsWith(draftPrefix)) sessionStorage.removeItem(key);
  } catch {
    /* Storage can be disabled by the browser. */
  }
}
export function exportReviewDraft(roundId: string, value: unknown): void {
  const url = URL.createObjectURL(
    new Blob(
      [typeof value === "string" ? value : JSON.stringify(value, null, 2)],
      { type: "application/json" },
    ),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `review-${roundId}.json`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
