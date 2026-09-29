/**
 * AsyncState + StatusBadge: unified loading / empty / error / read-only
 * presentation. State is always expressed with label text plus an icon,
 * never color alone. Errors keep a visible retry entry; transient toasts
 * are not used for persistent failures.
 */
import { Button, Spin } from "antd";
import styles from "./AsyncState.module.css";

export function AsyncLoading({
  label = "Loading…",
  testId,
}: {
  label?: string;
  testId?: string;
}): React.JSX.Element {
  return (
    <div className={styles.state} role="status" data-state="loading" data-testid={testId}>
      <Spin aria-hidden="true" />
      <p className={styles.stateTitle}>{label}</p>
    </div>
  );
}

export function AsyncEmpty({
  message,
  testId,
}: {
  message: string;
  testId?: string;
}): React.JSX.Element {
  return (
    <div className={styles.state} role="status" data-state="empty" data-testid={testId}>
      <p className={styles.stateTitle}>No results</p>
      <p>{message}</p>
    </div>
  );
}

export function AsyncError({
  message,
  onRetry,
  testId,
  retryTestId,
  retryLabel = "Retry",
}: {
  message: string;
  onRetry?: () => void;
  testId?: string;
  retryTestId?: string;
  retryLabel?: string;
}): React.JSX.Element {
  return (
    <div className={styles.state} role="alert" data-state="error" data-testid={testId}>
      <p className={styles.stateTitle}>Something went wrong</p>
      <p>{message}</p>
      {onRetry === undefined ? null : (
        <div className={styles.retry}>
          <Button onClick={onRetry} data-testid={retryTestId}>
            {retryLabel}
          </Button>
        </div>
      )}
    </div>
  );
}

export function AsyncReadOnly({ message }: { message: string }): React.JSX.Element {
  return (
    <div className={styles.state} role="status" data-state="readonly">
      <p className={styles.stateTitle}>Read-only</p>
      <p>{message}</p>
    </div>
  );
}

const BADGE_ICONS: Record<string, string> = {
  info: "●",
  success: "✓",
  warning: "⚠",
  error: "✕",
};

export function StatusBadge({
  tone = "info",
  label,
}: {
  tone?: "info" | "success" | "warning" | "error";
  label: string;
}): React.JSX.Element {
  return (
    <span className={styles.badge} data-tone={tone}>
      <span className={styles.badgeIcon} aria-hidden="true">
        {BADGE_ICONS[tone]}
      </span>
      <span>{label}</span>
    </span>
  );
}
