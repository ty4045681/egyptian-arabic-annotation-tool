import type { SaveStatus as SaveStatusValue } from "./types";
import styles from "./SaveStatus.module.css";

const STATUS_TEXT: Record<SaveStatusValue, { label: string; icon: string }> = {
  idle: { label: "Ready", icon: "○" },
  local: { label: "Unsaved changes", icon: "●" },
  syncing: { label: "Saving…", icon: "↻" },
  synced: { label: "Saved", icon: "✓" },
  offline: { label: "Waiting for connection", icon: "⚠" },
  conflict: { label: "Needs review", icon: "⚠" },
  frozen: { label: "Saving paused", icon: "⏸" },
  "storage-error": { label: "Local storage failed", icon: "⚠" },
  "terminal-unsupported": { label: "Continue in classic workspace", icon: "→" },
};

export function SaveStatusBadge({ status }: { status: SaveStatusValue }): React.JSX.Element {
  const entry = STATUS_TEXT[status];
  return (
    <span
      role="status"
      aria-label={`Save state: ${entry.label}`}
      data-state={status}
      data-save-state={status}
      data-testid="save-state"
      className={`${styles.badge} ${styles[status] ?? ""}`}
    >
      <span aria-hidden="true" className={styles.icon}>
        {entry.icon}
      </span>
      {entry.label}
    </span>
  );
}
