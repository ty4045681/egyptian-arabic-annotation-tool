/**
 * FilterBar: search/field grid with top labels and a reset entry.
 * CSS grid controls column widths and wrapping so long labels, multi
 * selects and date ranges wrap inside the bar instead of stretching the
 * page (U01). Advanced fields collapse behind `advanced` content.
 */
import { Button } from "antd";
import { useState, type ReactNode } from "react";
import styles from "./FilterBar.module.css";

export interface FilterField {
  key: string;
  label: ReactNode;
  control: ReactNode;
}

export function FilterBar({
  fields,
  advancedFields,
  onReset,
  resetLabel = "Reset",
  advancedLabel = "Advanced filters",
  notice,
  onRetry,
  retryLabel = "Retry",
  actions,
}: {
  fields: FilterField[];
  advancedFields?: FilterField[];
  onReset?: () => void;
  resetLabel?: string;
  advancedLabel?: string;
  /** Inline status shown above the grid (e.g. facet-load failure). */
  notice?: ReactNode;
  onRetry?: (() => void) | null;
  retryLabel?: string;
  actions?: ReactNode;
}): React.JSX.Element {
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const showAdvanced = (advancedFields?.length ?? 0) > 0;
  return (
    <section className={styles.bar} aria-label="Filters">
      {notice === undefined || notice === null ? null : (
        <p className={styles.notice} role="status">
          {notice}
          {onRetry ? (
            <Button type="link" size="small" onClick={onRetry}>
              {retryLabel}
            </Button>
          ) : null}
        </p>
      )}
      <div className={styles.grid}>
        {fields.map((field) => (
          <div className={styles.field} key={field.key}>
            <span className={styles.fieldLabel}>{field.label}</span>
            {field.control}
          </div>
        ))}
      </div>
      {showAdvanced && advancedOpen ? (
        <div className={styles.grid} style={{ marginTop: 12 }}>
          {(advancedFields ?? []).map((field) => (
            <div className={styles.field} key={field.key}>
              <span className={styles.fieldLabel}>{field.label}</span>
              {field.control}
            </div>
          ))}
        </div>
      ) : null}
      <div className={styles.footer}>
        {actions}
        {showAdvanced ? (
          <Button
            type="link"
            aria-expanded={advancedOpen}
            onClick={() => setAdvancedOpen((open) => !open)}
          >
            {advancedOpen ? "Hide advanced" : advancedLabel}
          </Button>
        ) : null}
        {onReset === undefined ? null : (
          <Button onClick={onReset}>{resetLabel}</Button>
        )}
      </div>
    </section>
  );
}
