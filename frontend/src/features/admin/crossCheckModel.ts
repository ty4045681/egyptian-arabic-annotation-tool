import type { CrossDetail, Segment } from "./schemas";

export type Manuscript = "original" | "secondary";
export type Decision = Manuscript | "edited";
export interface HighlightPart {
  text: string;
  changed: boolean;
  index: number | null;
}

export function highlightParts(
  segment: Segment,
  side: Manuscript,
  ops: NonNullable<CrossDetail["diff_ops"]>,
): HighlightPart[] {
  const points = Array.from(segment.text);
  const ranges = ops
    .flatMap((op, index) => {
      const location = op[side];
      return location &&
        String(location.segment_id) === String(segment.id) &&
        op.op !== "match"
        ? [{ start: location.text_start, end: location.text_end, index }]
        : [];
    })
    .sort((a, b) => a.start - b.start || a.end - b.end);
  const parts: HighlightPart[] = [];
  let cursor = 0;
  for (const range of ranges) {
    if (
      !Number.isInteger(range.start) ||
      !Number.isInteger(range.end) ||
      range.start < cursor ||
      range.end > points.length ||
      range.start > range.end
    ) {
      return [{ text: segment.text, changed: true, index: null }];
    }
    if (range.start > cursor)
      parts.push({
        text: points.slice(cursor, range.start).join(""),
        changed: false,
        index: null,
      });
    parts.push({
      text: points.slice(range.start, range.end).join(""),
      changed: true,
      index: range.index,
    });
    cursor = range.end;
  }
  if (cursor < points.length)
    parts.push({
      text: points.slice(cursor).join(""),
      changed: false,
      index: null,
    });
  return parts;
}

export function samplingBps(percent: string): number | null {
  if (!/^\d+(?:\.\d{1,2})?$/.test(percent.trim())) return null;
  const value = Number(percent);
  return value >= 0 && value <= 100 ? Math.round(value * 100) : null;
}

export function decisionLabel(decision: Decision): string {
  return decision === "edited"
    ? "Edit and publish"
    : decision === "original"
      ? "Use original"
      : "Use cross-check";
}

export const crossCheckFilterKeys = [
  "source_scene",
  "source_confidence",
  "batch_code",
  "original_annotator_id",
  "secondary_annotator_id",
  "reason_code",
  "q",
  "from",
  "to",
];
export const crossCheckReasons: {
  value: string;
  label: string;
  shortLabel?: string;
}[] = [
  {
    value: "word_difference_exceeded",
    label: "Word difference exceeds 10%",
    shortLabel: "Diff > 10%",
  },
  { value: "submission_status_conflict", label: "Submission status differs" },
  { value: "empty_original_text", label: "Original transcript is empty" },
  { value: "empty_secondary_text", label: "Cross-check transcript is empty" },
  { value: "bad_quality_conflict", label: "Bad quality coverage differs" },
  { value: "comparison_unavailable", label: "Comparison unavailable" },
];
