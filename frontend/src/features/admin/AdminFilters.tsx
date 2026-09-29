import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button, Input, Select, type InputRef } from "antd";
import {
  CloseOutlined,
  DownloadOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { applyDatePreset, type CorpusRange } from "./corpus/queryCodec";
import {
  dateOptions,
  filterToday,
  splitScenes,
  toggleScene,
  type DateSelection,
  type FilterChip,
  type FilterOption,
} from "./filterModel";
import "./admin-filters.css";

interface SearchProps {
  value: string;
  onChange: (value: string) => void;
  label: string;
  placeholder?: string;
  id?: string;
  testId?: string;
}

function FilterSearch({
  value,
  onChange,
  label,
  placeholder,
  id,
  testId,
}: SearchProps): React.JSX.Element {
  const [text, setText] = useState(value);
  const input = useRef<InputRef>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);
  useEffect(() => {
    clearTimeout(timer.current);
    setText(value);
  }, [value]);
  useEffect(() => {
    function focusSearch(event: KeyboardEvent) {
      const target = event.target;
      if (
        event.key !== "/" ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        event.isComposing
      )
        return;
      if (
        target instanceof Element &&
        target.closest(
          'input, textarea, select, [contenteditable="true"], [role="combobox"], [role="dialog"]',
        )
      )
        return;
      if (
        document.querySelector(
          '.ant-drawer-open, .ant-modal-wrap:not([style*="display: none"])',
        )
      )
        return;
      event.preventDefault();
      input.current?.focus();
    }
    document.addEventListener("keydown", focusSearch);
    return () => {
      clearTimeout(timer.current);
      document.removeEventListener("keydown", focusSearch);
    };
  }, []);
  function change(next: string, immediate = false) {
    setText(next);
    clearTimeout(timer.current);
    if (immediate || !next) onChangeRef.current(next);
    else timer.current = setTimeout(() => onChangeRef.current(next), 250);
  }
  return (
    <Input
      ref={input}
      id={id}
      data-testid={testId}
      className="af-search"
      aria-label={label}
      placeholder={placeholder ?? label}
      prefix={<SearchOutlined />}
      suffix={<kbd>/</kbd>}
      allowClear
      value={text}
      onChange={(event) => change(event.target.value)}
      onPressEnter={() => change(text, true)}
    />
  );
}

export function AdminFilterBar({
  search,
  primary,
  children,
  chips,
  summary,
  onReset,
  onExport,
  exportDisabled = false,
  label = "Filters",
}: {
  search?: SearchProps;
  primary?: ReactNode;
  children?: ReactNode;
  chips: FilterChip[];
  summary: ReactNode;
  onReset: () => void;
  onExport?: () => void;
  exportDisabled?: boolean;
  label?: string;
}): React.JSX.Element {
  const [reset, setReset] = useState(0);
  return (
    <section className="admin-filter-card" aria-label={label}>
      <div key={reset}>
        <div className="af-primary">
          {search && <FilterSearch {...search} />}
          <div className="af-primary-fields">{primary}</div>
          <div className="af-actions">
            <Button
              icon={<ReloadOutlined />}
              aria-label="Reset filters"
              onClick={() => {
                setReset((old) => old + 1);
                onReset();
              }}
            >
              Reset
            </Button>
            {onExport && (
              <Button
                type="primary"
                icon={<DownloadOutlined />}
                onClick={onExport}
                disabled={exportDisabled}
              >
                Export visible CSV
              </Button>
            )}
          </div>
        </div>
        {children}
      </div>
      <div className="af-footer">
        <div className="af-summary" role="status" aria-live="polite">
          {summary}
        </div>
        <div className="af-applied" aria-label="Applied filters">
          {chips.length ? (
            chips.map((chip) => (
              <span className="af-chip" key={chip.key}>
                <span>{chip.label}</span>
                <button
                  type="button"
                  aria-label={`Remove ${chip.label}`}
                  onClick={chip.onRemove}
                >
                  <CloseOutlined />
                </button>
              </span>
            ))
          ) : (
            <span className="af-empty">No filters applied</span>
          )}
        </div>
      </div>
    </section>
  );
}

export function FilterChoices({
  label,
  value,
  options,
  onChange,
  disabled = false,
  multiple = false,
  hint,
}: {
  label: string;
  value: string;
  options: readonly FilterOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  multiple?: boolean;
  hint?: string;
}): React.JSX.Element {
  const selected = splitScenes(value);
  return (
    <div
      className={`af-choice-group ${multiple ? "af-scene-group" : ""}`}
      role="group"
      aria-label={label}
    >
      <span className="af-label" title={hint}>
        {label}
      </span>
      <div className={multiple ? "af-scenes" : "af-segments"}>
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            disabled={disabled}
            aria-pressed={
              multiple
                ? selected.includes(option.value)
                : value === option.value
            }
            onClick={() =>
              onChange(
                multiple ? toggleScene(value, option.value) : option.value,
              )
            }
          >
            {option.color && (
              <span className="af-dot" style={{ background: option.color }} />
            )}
            {option.label}
            {option.count !== undefined && (
              <span className="af-count">{option.count.toLocaleString()}</span>
            )}
          </button>
        ))}
      </div>
      {multiple && <span className="af-hint">Select one or more</span>}
    </div>
  );
}

export function FilterField({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}): React.JSX.Element {
  return (
    <label className="af-field">
      <span>{label}</span>
      {children}
    </label>
  );
}

export function DateRangeFilter({
  value,
  onChange,
  today = filterToday(),
  prefix = "filter",
}: {
  value: DateSelection;
  onChange: (value: DateSelection) => void;
  today?: string;
  prefix?: string;
}): React.JSX.Element {
  const [draft, setDraft] = useState(value);
  const [error, setError] = useState("");
  const { range, from, to } = value;
  useEffect(() => {
    setDraft({ range, from, to });
    setError("");
  }, [range, from, to]);
  return (
    <div className="af-date">
      <Select
        aria-label="Date range"
        data-testid={`${prefix}-range`}
        value={draft.range}
        options={dateOptions}
        onChange={(range: CorpusRange) => {
          const next = { range, ...applyDatePreset(range, today, draft) };
          setDraft(next);
          setError("");
          if (range !== "custom") onChange(next);
        }}
      />
      {draft.range === "custom" && (
        <div className="af-date-custom">
          <Input
            type="date"
            id={`${prefix}From`}
            aria-label="From date, inclusive"
            data-testid={`${prefix}-from`}
            value={draft.from}
            onChange={(event) =>
              setDraft({ ...draft, from: event.target.value })
            }
          />
          <span>to</span>
          <Input
            type="date"
            id={`${prefix}To`}
            aria-label="To date, inclusive"
            data-testid={`${prefix}-to`}
            value={draft.to}
            onChange={(event) => setDraft({ ...draft, to: event.target.value })}
          />
          <Button
            data-testid={`${prefix}-apply-dates`}
            onClick={() => {
              if (!draft.from || !draft.to || draft.from > draft.to) {
                setError("Choose a valid start and end date.");
                return;
              }
              setError("");
              onChange(draft);
            }}
          >
            Apply dates
          </Button>
          {error && (
            <span className="af-date-error" role="alert">
              {error}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
