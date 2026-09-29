import {
  confidenceOptions,
  splitScenes,
  toggleScene,
  type FilterChip,
  type FilterOption,
} from "./filterModel";
import type { Filters } from "./api";
export const metadataKeys = [
  "source_scene",
  "source_confidence",
  "batch_code",
  "review_status",
  "prediction_scene",
  "human_scene",
];
export const advanced = [
  { key: "batch_code", label: "Source batch", testId: "corpus-batch" },
  {
    key: "review_status",
    label: "Human review status",
    testId: "corpus-review-status",
  },
  {
    key: "prediction_scene",
    label: "Model classification",
    testId: "corpus-prediction-scene",
  },
  {
    key: "human_scene",
    label: "Human review scene",
    testId: "corpus-human-scene",
  },
];
const reviewOptions = [
  { value: "pending", label: "Pending (published)" },
  { value: "confirmed", label: "Confirmed" },
  { value: "mixed", label: "Multiple scenes" },
  { value: "out_of_scope", label: "Outside the ten scenes" },
  { value: "uncertain", label: "Cannot determine" },
  { value: "unreviewed_published", label: "Published, not reviewed" },
  { value: "unreviewed_unpublished", label: "Unpublished, not reviewed" },
];
export type MetadataOptions = {
  scenes: FilterOption[];
  batches: FilterOption[];
};
export function optionsFor(
  key: string,
  options: MetadataOptions,
): FilterOption[] {
  if (key === "batch_code") return options.batches;
  if (key === "review_status") return reviewOptions;
  return [
    {
      value: "unknown",
      label:
        key === "human_scene" ? "No human scene" : "No model classification",
    },
    ...options.scenes,
  ];
}
export function metadataChips(
  values: Filters,
  options: MetadataOptions,
  apply: (values: Filters) => void,
): FilterChip[] {
  const chips: FilterChip[] = splitScenes(values.source_scene ?? "").map(
    (value) => ({
      key: `scene:${value}`,
      label:
        options.scenes.find((item) => item.value === value)?.label ?? value,
      onRemove: () =>
        apply({ source_scene: toggleScene(values.source_scene ?? "", value) }),
    }),
  );
  if (values.source_confidence)
    chips.push({
      key: "confidence",
      label: `Confidence: ${confidenceOptions.find((item) => item.value === values.source_confidence)?.label ?? values.source_confidence}`,
      onRemove: () => apply({ source_confidence: "" }),
    });
  for (const field of advanced) {
    const value = values[field.key];
    if (value)
      chips.push({
        key: field.key,
        label: `${field.label}: ${optionsFor(field.key, options).find((item) => item.value === value)?.label ?? value}`,
        onRemove: () => apply({ [field.key]: "" }),
      });
  }
  return chips;
}
