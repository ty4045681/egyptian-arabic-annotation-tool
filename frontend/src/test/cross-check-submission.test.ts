import { describe, expect, it } from "vitest";
import {
  reviewForVerdict,
  sceneReviewsAgree,
  sceneVerdict,
} from "../features/admin/crossCheckSubmissionModel";
import type { SceneReview } from "../features/admin/schemas";

const pending: SceneReview = {
  status: "pending",
  scene_codes: [],
  note: "Listen to the background",
};

describe("submission scene decisions", () => {
  it("distinguishes unreviewed evidence from an uncertain human review", () => {
    expect(sceneReviewsAgree(null, pending)).toBe(true);
    expect(
      sceneReviewsAgree(pending, { ...pending, status: "uncertain" }),
    ).toBe(false);
    expect(
      sceneReviewsAgree(
        { ...pending, status: "mixed", scene_codes: ["airport", "hotel"] },
        { ...pending, status: "mixed", scene_codes: ["hotel", "airport"] },
      ),
    ).toBe(true);
  });
  it("confirms the real source scene and preserves the review note", () => {
    expect(reviewForVerdict(pending, "match", ["airport"])).toEqual({
      status: "confirmed",
      scene_codes: ["airport"],
      note: "Listen to the background",
    });
  });
  it("requires a new scene choice for reassignment instead of retaining the source", () => {
    const confirmed = {
      ...pending,
      status: "confirmed",
      scene_codes: ["airport"],
    };
    expect(sceneVerdict(confirmed, ["airport"])).toBe("match");
    expect(sceneVerdict(confirmed, ["airport", "hotel"])).toBe("reassign");
    expect(sceneVerdict(confirmed, [])).toBe("reassign");
    expect(reviewForVerdict(confirmed, "reassign", ["airport"])).toEqual({
      ...pending,
      status: "confirmed",
      scene_codes: [],
    });
  });
  it.each(["pending", "uncertain", "out_of_scope"] as const)(
    "clears old scene labels for %s",
    (verdict) => {
      expect(
        reviewForVerdict(
          { ...pending, status: "mixed", scene_codes: ["airport", "hotel"] },
          verdict,
          ["airport"],
        ),
      ).toEqual({ ...pending, status: verdict });
    },
  );
});
