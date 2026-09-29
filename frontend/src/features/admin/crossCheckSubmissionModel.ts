import type { SceneReview } from "./schemas";

export type SceneVerdict =
  | "match"
  | "reassign"
  | "mixed"
  | "out_of_scope"
  | "uncertain"
  | "pending";

export function sameValues(a: string[], b: string[]): boolean {
  const sortedB = [...b].sort();
  return (
    a.length === b.length &&
    [...a].sort().every((value, index) => value === sortedB[index])
  );
}

export function sceneReviewsAgree(
  a: SceneReview | null,
  b: SceneReview | null,
): boolean {
  return (
    (a?.status ?? "pending") === (b?.status ?? "pending") &&
    sameValues(a?.scene_codes ?? [], b?.scene_codes ?? [])
  );
}

export function sceneVerdict(
  review: SceneReview,
  sourceCodes: string[],
): SceneVerdict {
  switch (review.status) {
    case "confirmed":
      return sourceCodes.length === 1 &&
        sameValues(review.scene_codes, sourceCodes)
        ? "match"
        : "reassign";
    case "mixed":
    case "out_of_scope":
    case "uncertain":
    case "pending":
      return review.status;
    default:
      return "pending";
  }
}

export function reviewForVerdict(
  review: SceneReview,
  verdict: SceneVerdict,
  sourceCodes: string[],
): SceneReview {
  switch (verdict) {
    case "match":
      return { ...review, status: "confirmed", scene_codes: [...sourceCodes] };
    case "reassign":
      return { ...review, status: "confirmed", scene_codes: [] };
    case "mixed":
      return { ...review, status: "mixed" };
    case "pending":
    case "uncertain":
    case "out_of_scope":
      return { ...review, status: verdict, scene_codes: [] };
    default: {
      const exhaustive: never = verdict;
      return exhaustive;
    }
  }
}
