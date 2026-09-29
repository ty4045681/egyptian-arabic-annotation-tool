import { Button, Checkbox, Input, Radio } from "antd";
import { useState } from "react";
import {
  blankReview,
  reviewValidation,
  type SceneReviewValue,
  type SceneTaxonomyEntry,
} from "./sceneReview";
import styles from "./SceneReviewForm.module.css";

const STATUS_OPTIONS: Array<{ value: SceneReviewValue["status"]; label: string }> = [
  { value: "pending", label: "Pending" },
  { value: "confirmed", label: "Confirm" },
  { value: "mixed", label: "Mixed / multiple scenes" },
];

export function SceneReviewForm({
  value,
  taxonomy,
  disabled,
  writeEnabled,
  onChange,
}: {
  value: SceneReviewValue | null;
  taxonomy: SceneTaxonomyEntry[];
  disabled: boolean;
  /** When false the controls are read-only and cannot emit changes (W05). */
  writeEnabled: boolean;
  onChange: (next: SceneReviewValue) => void;
}): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const working = value ?? blankReview();
  const validation = reviewValidation(working);
  // Feature-gated: a closed write flag must not allow edits that can never
  // be saved (W05). Controls render read-only instead.
  const locked = disabled || !writeEnabled;

  const emit = (next: SceneReviewValue): void => {
    if (locked) return;
    onChange(next);
  };

  return (
    <section aria-label="Scene verification" data-testid="scene-review" className={styles.panel}>
      <Button
        type="link"
        block
        aria-expanded={open}
        onClick={() => setOpen((previous) => !previous)}
        className={styles.toggle ?? ""}
      >
        {open ? "Hide scene verification" : "Show scene verification"}
      </Button>
      {open ? (
        <div className={styles.body}>
          {!writeEnabled ? (
            <p className={styles.hint} role="status">
              Scene verification is read-only for this task.
            </p>
          ) : null}
          <Radio.Group
            aria-label="Scene status"
            value={working.status}
            disabled={locked}
            onChange={(event) =>
              emit({ ...working, status: event.target.value as SceneReviewValue["status"] })
            }
            options={STATUS_OPTIONS.map((option) => ({
              value: option.value,
              label: option.label,
            }))}
          />
          <fieldset className={styles.scenes}>
            <legend>Scenes</legend>
            {taxonomy.length === 0 ? (
              <p className={styles.hint}>Scene options are unavailable.</p>
            ) : (
              <Checkbox.Group
                aria-label="Scenes"
                value={working.scene_codes}
                disabled={locked}
                onChange={(codes) =>
                  emit({ ...working, scene_codes: codes.map(String) })
                }
                options={taxonomy.map((entry) => ({
                  value: entry.code,
                  label: entry.label_en ?? entry.label ?? entry.code,
                }))}
              />
            )}
          </fieldset>
          <label className={styles.noteLabel}>
            Reviewer note
            <Input.TextArea
              dir="auto"
              value={working.note}
              disabled={locked}
              rows={2}
              aria-label="Scene review note"
              onChange={(event) => emit({ ...working, note: event.target.value })}
              className={styles.note ?? ""}
            />
          </label>
          {!validation.ok ? (
            <p role="alert" className={styles.error}>
              {validation.reason}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
