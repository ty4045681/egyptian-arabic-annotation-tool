/**
 * Segment time formatting/parsing (work package F, workspace/editor).
 *
 * Pure helpers shared by SegmentList rows. Times render as `MM:SSS.mmm`
 * (LTR-isolated in the UI) and parse back from either `MM:SS.mmm` or plain
 * seconds. Invalid input yields null so callers restore the stored value
 * instead of writing a corrupt boundary.
 */

export function formatTime(seconds: number): string {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const minutes = Math.floor(safe / 60);
  const rest = safe - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${rest.toFixed(3).padStart(6, "0")}`;
}

export function parseTimeInput(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parts = trimmed.split(":");
  let result: number;
  if (parts.length === 2) {
    const [minutesPart, secondsPart] = parts;
    // Complete syntax only: both parts must be fully numeric. Rejects
    // `00:` / `00:1.` / `not-a-time` so Number("") === 0 cannot turn an
    // unfinished value into a valid 0-second boundary (Y08).
    if (!/^\d+$/.test(minutesPart ?? "") || !/^\d+(\.\d+)?$/.test(secondsPart ?? "")) {
      return null;
    }
    result = Number(minutesPart) * 60 + Number(secondsPart);
  } else if (parts.length === 1) {
    if (!/^\d+(\.\d+)?$/.test(parts[0] ?? "")) return null;
    result = Number(parts[0]);
  } else {
    return null;
  }
  return Number.isFinite(result) && result >= 0 ? result : null;
}

export interface TimeBounds {
  min: number;
  max: number;
}

/**
 * Clamp a parsed time to the segment's legal range (X05). `start` must stay
 * below the segment end; `end` must stay above the start and (for the last
 * segment) within the audio duration. Shared by the row editor and the
 * controller's pending-edit commit so both agree.
 */
export function clampTimeEdit(
  field: "start" | "end",
  parsed: number,
  current: { start: number; end: number },
  bounds: TimeBounds,
): number {
  let next = parsed;
  if (field === "start") {
    next = Math.max(bounds.min, Math.min(next, current.end - 0.001));
  } else {
    next = Math.min(bounds.max, Math.max(next, current.start + 0.001));
  }
  return Math.round(next * 1000) / 1000;
}
