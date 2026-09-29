/**
 * Corpus match totals. Async states now come from the shared
 * `src/components/AsyncState` (single component system); this file keeps
 * only the corpus-specific totals formatting.
 */
import { formatCorpusDuration } from "./corpusFormat";

export interface MatchedStatsProps {
  count: number;
  durationSeconds: number;
}

/**
 * Match totals come straight from `matched_count` /
 * `matched_duration_seconds` of the list response — never from the loaded
 * row count and never from a separate overview call.
 */
export function MatchedStats({
  count,
  durationSeconds,
}: MatchedStatsProps): React.JSX.Element {
  const formatted = new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 0,
  }).format(count);
  return (
    <p role="status" data-testid="corpus-matched" style={{ margin: "0 0 8px" }}>
      <strong>{`${formatted} tasks`}</strong>
      {" · "}
      <span>{`${formatCorpusDuration(durationSeconds)} source audio`}</span>
    </p>
  );
}
