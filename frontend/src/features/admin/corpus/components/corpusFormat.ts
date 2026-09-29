import type { CorpusTaskItem } from "../types";

export function formatCorpusDuration(totalSeconds: number): string {
  const total = Number.isFinite(totalSeconds)
    ? Math.max(0, Math.round(totalSeconds))
    : 0;
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 60) return `${minutes}m ${seconds}s`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return `${hours}h ${rest}m`;
}

/** Submitter/assignee display: assignment holder wins, then publisher. */
export function corpusAnnotatorName(item: CorpusTaskItem): string {
  return (
    item.assignment?.username ||
    item.current_submitter?.username ||
    item.submitted_by ||
    "—"
  );
}
