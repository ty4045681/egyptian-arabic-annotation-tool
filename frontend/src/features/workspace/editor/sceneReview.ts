/**
 * Minimal scene-review input model (work package F, workspace/editor).
 *
 * P1 covers only the smallest review input combination needed to prove the
 * save protocol (status + scene codes + note); the full scene UI migrates in
 * P4. Semantics mirror index.html: review is optional, reviewDirty tracks an
 * unconfirmed review-only change, and when the `scene_review_write` feature
 * flag is off the transcript still saves without sending scene_review.
 */

export type SceneReviewStatus = "pending" | "confirmed" | "mixed";

export interface SceneReviewValue {
  status: SceneReviewStatus;
  scene_codes: string[];
  note: string;
}

export interface SceneTaxonomyEntry {
  code: string;
  label_en?: string;
  label?: string;
}

export interface FeatureFlags {
  metadata_ui?: boolean;
  scene_review_write?: boolean;
  [flag: string]: unknown;
}

export function blankReview(): SceneReviewValue {
  return { status: "pending", scene_codes: [], note: "" };
}

export function cloneReview(value: SceneReviewValue | null | undefined): SceneReviewValue | null {
  if (value === null || value === undefined) return null;
  return {
    status: value.status,
    scene_codes: [...(value.scene_codes ?? [])],
    note: value.note ?? "",
  };
}

export function reviewValidation(review: SceneReviewValue | null | undefined): {
  ok: boolean;
  reason?: string;
} {
  if (!review) return { ok: false, reason: "missing review" };
  if (review.status !== "pending" && review.status !== "confirmed" && review.status !== "mixed") {
    return { ok: false, reason: "unknown status" };
  }
  if (!Array.isArray(review.scene_codes)) return { ok: false, reason: "scene codes must be an array" };
  if (review.status !== "pending" && review.scene_codes.length === 0) {
    return { ok: false, reason: "confirmed reviews need at least one scene" };
  }
  return { ok: true };
}

export function reviewWriteEnabled(features: FeatureFlags | null | undefined): boolean {
  if (!features) return false;
  if (features.metadata_ui === false) return false;
  return features.scene_review_write !== false;
}

/**
 * Metadata UI visibility (W05). When `metadata_ui` is off the whole scene
 * verification block is hidden, matching the legacy page. Absent flag means
 * enabled (P1 default).
 */
export function metadataUiEnabled(features: FeatureFlags | null | undefined): boolean {
  if (!features) return true;
  return features.metadata_ui !== false;
}

/** A review-only change is saveable even when no transcript row is dirty. */
export function shouldSaveReview(
  features: FeatureFlags | null | undefined,
  reviewDirty: boolean,
  review: SceneReviewValue | null | undefined,
): boolean {
  return Boolean(reviewDirty && reviewWriteEnabled(features) && reviewValidation(review).ok);
}

export function reviewsEqual(
  left: SceneReviewValue | null | undefined,
  right: SceneReviewValue | null | undefined,
): boolean {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}
