import {
  dateChip,
  confidenceOptions,
  splitScenes,
  toggleScene,
  type DateSelection,
  type FilterChip,
} from "./filterModel";
import { useState, type ReactNode } from "react";
import { Alert, Button, Select } from "antd";
import { useSearchParams } from "react-router-dom";
import {
  type Filters,
  type useAdminQuery,
  type useAnnotatorDirectory,
} from "./api";
import type { z } from "zod";
import type { facetsSchema } from "./schemas";
import {
  AdminFilterBar,
  DateRangeFilter,
  FilterChoices,
  FilterField,
} from "./AdminFilters";
import { crossCheckFilterKeys, crossCheckReasons } from "./crossCheckModel";

export function CrossCheckFilters({
  filters,
  facets,
  personOptions,
  people,
  states,
  dates,
  activeState,
  summary,
  onExport,
  exportDisabled,
}: {
  filters: Filters;
  facets: ReturnType<typeof useAdminQuery<z.infer<typeof facetsSchema>>>;
  personOptions: { value: string; label: string }[];
  people: ReturnType<typeof useAnnotatorDirectory>;
  states: ReactNode;
  dates: DateSelection;
  activeState: string;
  summary: ReactNode;
  onExport: () => void;
  exportDisabled: boolean;
}): React.JSX.Element {
  const [, setSearch] = useSearchParams();
  const [expanded, setExpanded] = useState(
    Boolean(
      filters.batch_code ||
        filters.reason_code ||
        filters.original_annotator_id,
    ),
  );
  function apply(values: Filters, replace = false) {
    setSearch(
      (old) => {
        const next = new URLSearchParams(old);
        next.delete("round");
        for (const [key, value] of Object.entries(values)) {
          if (value) next.set(`cc_${key}`, value);
          else next.delete(`cc_${key}`);
        }
        return next;
      },
      { replace },
    );
  }
  function setDates(next: DateSelection) {
    apply({
      range: next.range,
      from: next.range === "custom" ? next.from : "",
      to: next.range === "custom" ? next.to : "",
    });
  }
  const sceneOptions =
    facets.data?.scenes.map((scene) => ({
      value: scene.code,
      label: scene.label_en,
    })) ?? [];
  const chips: FilterChip[] = [
    ...dateChip(dates, () => setDates({ range: "all", from: "", to: "" })),
    ...(activeState !== "all"
      ? [
          {
            key: "state",
            label: `Status: ${activeState.replaceAll("_", " ")}`,
            onRemove: () => apply({ state: "all" }),
          },
        ]
      : []),
    ...splitScenes(filters.source_scene ?? "").map((scene) => ({
      key: `scene:${scene}`,
      label:
        sceneOptions.find((option) => option.value === scene)?.label ?? scene,
      onRemove: () =>
        apply({ source_scene: toggleScene(filters.source_scene ?? "", scene) }),
    })),
  ];
  for (const [key, name] of [
    ["q", "Search"],
    ["source_confidence", "Confidence"],
    ["reason_code", "Reason"],
    ["batch_code", "Batch"],
    ["original_annotator_id", "Original annotator"],
    ["secondary_annotator_id", "Cross-check annotator"],
  ]) {
    if (!key) continue;
    const value = filters[key];
    if (!value) continue;
    const display = key.endsWith("annotator_id")
      ? personOptions.find((option) => option.value === value)?.label
      : key === "reason_code"
        ? crossCheckReasons.find((option) => option.value === value)?.label
        : value;
    chips.push({
      key,
      label: `${name}: ${display ?? value}`,
      onRemove: () => apply({ [key]: "" }),
    });
  }
  return (
    <AdminFilterBar
      label="Filter cross-check rounds"
      search={{
        value: filters.q ?? "",
        onChange: (q) => apply({ q }, true),
        label: "Search cross-checks",
        id: "ccSearch",
        placeholder: "Search filename, path or ID…",
      }}
      primary={
        <>
          <DateRangeFilter value={dates} onChange={setDates} prefix="cc" />
          <Select
            aria-label="Cross-check annotator"
            placeholder="All cross-check annotators"
            allowClear
            showSearch={{ optionFilterProp: "label" }}
            value={filters.secondary_annotator_id || undefined}
            onChange={(value: string | undefined) =>
              apply({ secondary_annotator_id: value ?? "" })
            }
            options={personOptions}
            loading={people.isFetching}
            disabled={!people.data || people.isError}
          />
        </>
      }
      chips={chips}
      summary={summary}
      onExport={onExport}
      exportDisabled={exportDisabled}
      onReset={() => {
        setExpanded(false);
        apply(
          Object.fromEntries(
            [...crossCheckFilterKeys, "range", "state"].map((key) => [key, ""]),
          ),
        );
      }}
    >
      <div className="af-row">{states}</div>
      <div className="af-row">
        <FilterChoices
          label="Confidence"
          value={filters.source_confidence ?? ""}
          options={confidenceOptions}
          disabled={!facets.data || facets.isError}
          onChange={(source_confidence) => apply({ source_confidence })}
        />
      </div>
      <div className="af-row">
        <FilterChoices
          label="Scene"
          multiple
          value={filters.source_scene ?? ""}
          options={sceneOptions}
          disabled={!facets.data || facets.isError}
          onChange={(source_scene) => apply({ source_scene })}
        />
      </div>
      <div className="af-advanced">
        <Button
          type="text"
          aria-label="More filters"
          aria-expanded={expanded}
          aria-controls="ccExtraFilters"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Hide advanced filters" : "More filters"}
        </Button>
        {expanded && (
          <div className="af-row" id="ccExtraFilters">
            <FilterField label="Original annotator">
              <Select
                aria-label="Original annotator"
                placeholder="All original annotators"
                allowClear
                showSearch={{ optionFilterProp: "label" }}
                value={filters.original_annotator_id || undefined}
                onChange={(value: string | undefined) =>
                  apply({ original_annotator_id: value ?? "" })
                }
                options={personOptions}
                loading={people.isFetching}
                disabled={!people.data || people.isError}
              />
            </FilterField>
            <FilterField label="Reason">
              <Select
                aria-label="Reason"
                placeholder="All reasons"
                allowClear
                value={filters.reason_code || undefined}
                options={crossCheckReasons}
                onChange={(value: string | undefined) =>
                  apply({ reason_code: value ?? "" })
                }
                popupMatchSelectWidth={280}
              />
            </FilterField>
            <FilterField label="Batch">
              <Select
                aria-label="Batch code"
                placeholder="All batches"
                allowClear
                showSearch={{ optionFilterProp: "label" }}
                value={filters.batch_code || undefined}
                disabled={!facets.data || facets.isError}
                onChange={(value: string | undefined) =>
                  apply({ batch_code: value ?? "" })
                }
                options={
                  facets.data?.batches.map((batch) => ({
                    value: batch.batch_code,
                    label: batch.batch_code,
                  })) ?? []
                }
              />
            </FilterField>
          </div>
        )}
      </div>
      {facets.isError && (
        <Alert
          type="error"
          title="Filter options unavailable"
          action={
            <Button onClick={() => void facets.refetch()}>Retry catalog</Button>
          }
        />
      )}
      {people.isError && (
        <Alert
          type="error"
          title="Annotator options could not be fully loaded"
          action={
            <Button onClick={() => void people.refetch()}>
              Retry annotators
            </Button>
          }
        />
      )}
    </AdminFilterBar>
  );
}
