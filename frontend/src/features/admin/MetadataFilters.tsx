import type { ReactNode } from "react";
import { Select } from "antd";
import { FilterChoices, FilterField } from "./AdminFilters";
import { confidenceOptions } from "./filterModel";
import {
  advanced,
  optionsFor,
  type MetadataOptions,
} from "./metadataFilterModel";
import type { Filters } from "./api";

export function MetadataFilters({
  values,
  options,
  apply,
  disabled,
  counts,
  leading,
}: {
  values: Filters;
  options: MetadataOptions;
  apply: (values: Filters) => void;
  disabled: boolean;
  leading?: ReactNode;
  counts?:
    | { confidence: Record<string, number>; scenes: Record<string, number> }
    | undefined;
}): React.JSX.Element {
  return (
    <>
      <div className="af-row">
        {leading}
        <FilterChoices
          label="Confidence"
          value={values.source_confidence ?? ""}
          disabled={disabled}
          options={confidenceOptions.map((item) => ({
            ...item,
            count:
              item.value && counts
                ? (counts.confidence[item.value] ?? 0)
                : undefined,
          }))}
          onChange={(source_confidence) => apply({ source_confidence })}
        />
      </div>
      <div className="af-row">
        <FilterChoices
          label="Scene"
          multiple
          value={values.source_scene ?? ""}
          disabled={disabled}
          options={options.scenes.map((item) => ({
            ...item,
            count: counts ? (counts.scenes[item.value] ?? 0) : undefined,
          }))}
          onChange={(source_scene) => apply({ source_scene })}
        />
      </div>
      <details
        className="af-advanced"
        open={advanced.some((field) => values[field.key]) || undefined}
      >
        <summary>
          Advanced filters
          {advanced.filter((field) => values[field.key]).length
            ? ` (${advanced.filter((field) => values[field.key]).length})`
            : ""}
        </summary>
        <div className="af-row">
          {advanced.map((field) => (
            <FilterField key={field.key} label={field.label}>
              <Select
                aria-label={field.label}
                data-testid={field.testId}
                allowClear
                showSearch={{ optionFilterProp: "label" }}
                placeholder={`All ${field.label.toLowerCase()}`}
                value={values[field.key] || undefined}
                disabled={disabled}
                onChange={(value: string | undefined) =>
                  apply({ [field.key]: value ?? "" })
                }
                options={optionsFor(field.key, options)}
              />
            </FilterField>
          ))}
        </div>
      </details>
    </>
  );
}
