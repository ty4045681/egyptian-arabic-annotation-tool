import { describe, expect, it } from "vitest";
import {
  emptyReviewDraft,
  pickReviewSegment,
  reviewDecisionBody,
  reviewDraftSchema,
  reviewRows,
  unresolvedRows,
} from "../features/admin/crossCheckReviewModel";
import type { Segment } from "../features/admin/schemas";

const original: Segment = {
  id: 1,
  start: 0,
  end: 5,
  duration: 5,
  text: "Original words",
  exclude_from_training: false,
};
const second: Segment = {
  id: 2,
  start: 5,
  end: 10,
  duration: 5,
  text: "",
  exclude_from_training: true,
};
const secondary: Segment = {
  ...original,
  text: "Independent transcript",
  exclude_from_training: true,
};
const evidence = {
  original_segments: [original, second],
  secondary_segments: [secondary, second],
  original_target_status: "annotated",
  secondary_target_status: "annotated",
  original_skip_reasons: [],
  secondary_skip_reasons: [],
};
const comparison = {
  ...evidence,
  comparison_unavailable: false,
  diff_ops: [
    {
      op: "replace",
      original: {
        segment_id: 1,
        text_start: 0,
        text_end: 8,
        start_s: 0,
        end_s: 5,
      },
      secondary: {
        segment_id: 1,
        text_start: 0,
        text_end: 11,
        start_s: 0,
        end_s: 5,
      },
    },
  ],
};

describe("segment review decisions", () => {
  it("sends only editable scene-review fields to the decision API", () => {
    const row = reviewRows(comparison)[0];
    if (!row) throw new Error("Expected a reviewable segment");
    const draft = pickReviewSegment(
      evidence,
      emptyReviewDraft(),
      row,
      "original",
    );
    const result = reviewDecisionBody(
      {
        revision: 2,
        original_version_id: "original",
        secondary_version_id: "secondary",
      },
      {
        ...draft,
        override: true,
        review: {
          id: "immutable-evidence-id",
          status: "confirmed",
          scene_codes: ["airport"],
          note: "Reviewed scene",
        },
      },
    );
    expect(result.scene_review).toEqual({
      status: "confirmed",
      scene_codes: ["airport"],
      note: "Reviewed scene",
    });
  });

  it("publishes all base segments while copying only the chosen segment and its quality flag", () => {
    const rows = reviewRows(comparison);
    const row = rows[0];
    if (!row) throw new Error("Expected a reviewable segment");
    const draft = pickReviewSegment(
      evidence,
      emptyReviewDraft(),
      row,
      "secondary",
    );
    expect(unresolvedRows(rows, draft)).toEqual([]);
    expect(
      reviewDecisionBody(
        {
          revision: 4,
          original_version_id: "original",
          secondary_version_id: "secondary",
        },
        { ...draft, reason: "  Listened to both versions  " },
      ),
    ).toEqual({
      expected_revision: 4,
      expected_original_version_id: "original",
      expected_secondary_version_id: "secondary",
      decision: "edited",
      reason: "Listened to both versions",
      base: "original",
      target_status: "annotated",
      skip_reasons: [],
      segments: [
        {
          id: 1,
          start: 0,
          end: 5,
          duration: 5,
          text: "Independent transcript",
          exclude_from_training: true,
        },
        {
          id: 2,
          start: 5,
          end: 10,
          duration: 5,
          text: "",
          exclude_from_training: true,
        },
      ],
    });
    expect(original.text).toBe("Original words");
    expect(original.exclude_from_training).toBe(false);
  });
  it("requires review for quality-only disagreements", () => {
    const rows = reviewRows({
      ...comparison,
      diff_ops: [],
      secondary_segments: [
        { ...original, exclude_from_training: true },
        second,
      ],
    });
    expect(
      unresolvedRows(rows, emptyReviewDraft()).map((row) => row.id),
    ).toEqual(["1"]);
  });
  it("does not move words between incompatible segment boundaries", () => {
    const other = { ...secondary, start: 2, end: 6 };
    const changed = { ...comparison, secondary_segments: [other, second] };
    const row = reviewRows(changed)[0];
    if (!row) throw new Error("Expected the incompatible segment");
    const draft = pickReviewSegment(
      changed,
      emptyReviewDraft(),
      row,
      "secondary",
    );
    expect(draft.editor).toBeNull();
    expect(row.compatible).toBe(false);
  });
  it("keeps secondary-only segments visible instead of discarding evidence", () => {
    const rows = reviewRows({
      ...comparison,
      secondary_segments: [
        secondary,
        second,
        { ...original, id: 3, start: 10, end: 12, duration: 2 },
      ],
    });
    expect(
      rows.map((row) => [
        row.id,
        row.original?.id ?? null,
        row.secondary?.id ?? null,
      ]),
    ).toEqual([
      ["1", 1, 1],
      ["2", 2, 2],
      ["3", null, 3],
    ]);
    expect(
      unresolvedRows(rows, emptyReviewDraft()).map((row) => row.id),
    ).toEqual(["1", "3"]);
  });
  it("requires segment choices when restoring an old whole-transcript draft", () => {
    const rows = reviewRows(comparison);
    const row = rows[0];
    if (!row) throw new Error("Expected a reviewable segment");
    const saved = pickReviewSegment(evidence, emptyReviewDraft(), row, "secondary");
    const restored = reviewDraftSchema.parse({
      ...saved,
      decision: "secondary",
      editAll: true,
      picks: {},
      reason: "Saved before segment-only review",
    });
    expect(unresolvedRows(rows, restored).map((item) => item.id)).toEqual(["1"]);
    expect(restored.editor?.segments[0]?.text).toBe("Independent transcript");
    expect(restored.reason).toBe("Saved before segment-only review");
  });
});
