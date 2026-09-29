import type { CorpusRange } from "./corpus/queryCodec";
export interface FilterChip {
  key: string;
  label: string;
  onRemove: () => void;
}
export interface FilterOption {
  value: string;
  label: string;
  count?: number | undefined;
  color?: string | undefined;
}
export function splitScenes(value: string): string[] {
  return [
    ...new Set(
      value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ].sort();
}
export function toggleScene(value: string, scene: string): string {
  const scenes = splitScenes(value);
  return (
    scenes.includes(scene)
      ? scenes.filter((item) => item !== scene)
      : [...scenes, scene]
  )
    .sort()
    .join(",");
}
export const confidenceOptions: readonly FilterOption[] = [
  { value: "", label: "Any" },
  { value: "high", label: "High", color: "#22a06b" },
  { value: "medium", label: "Medium", color: "#df9914" },
  { value: "low", label: "Low", color: "#d86161" },
  { value: "unknown", label: "Unknown", color: "#94a3b8" },
];
export const dateOptions: { value: CorpusRange; label: string }[] = [
  { value: "7d", label: "Last 7 days" },
  { value: "30d", label: "Last 30 days" },
  { value: "90d", label: "Last 90 days" },
  { value: "all", label: "All time" },
  { value: "custom", label: "Custom range" },
];
export function filterToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}
export type DateSelection = { range: CorpusRange; from: string; to: string };
export function dateChip(
  value: DateSelection,
  remove: () => void,
): FilterChip[] {
  return value.range === "all"
    ? []
    : [
        {
          key: "dates",
          label:
            value.range === "custom"
              ? `${value.from} – ${value.to}`
              : (dateOptions.find((option) => option.value === value.range)
                  ?.label ?? value.range),
          onRemove: remove,
        },
      ];
}
