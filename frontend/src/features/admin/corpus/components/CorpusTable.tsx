import { Button, type TableColumnsType } from "antd";
import { useState } from "react";
import { AdminListFooter, AdminTable } from "../../AdminList";
import { CORPUS_TIMEZONE } from "../queryCodec";
import type { CorpusTaskItem } from "../types";
import { corpusAnnotatorName, formatCorpusDuration } from "./corpusFormat";
import styles from "../CorpusPage.module.css";
import { Status } from "../../AdminParts";

export interface CorpusTableProps {
  items: CorpusTaskItem[];
  sceneLabels: Record<string, string>;
  /** Server cursor append affordances (never fabricated page numbers). */
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
  appendError?: string | null;
  onRetryAppend?: () => void;
  onViewTask?: ((taskId: string) => void) | undefined;
  onRevoke?: ((task: CorpusTaskItem) => void) | undefined;
}

const CONFIDENCE_LABELS: Record<string, string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
  unknown: "Unknown",
};

const REVIEW_STATUS_LABELS: Record<string, string> = {
  pending: "Pending (published)",
  confirmed: "Confirmed",
  mixed: "Multiple scenes",
  out_of_scope: "Outside the ten scenes",
  uncertain: "Cannot determine",
  unreviewed_unpublished: "Unpublished, not reviewed",
  unreviewed_published: "Published, not reviewed",
};

function formatCorpusDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: CORPUS_TIMEZONE,
  }).format(date);
}

function sceneLabel(code: string, sceneLabels: Record<string, string>): string {
  if (!code) return "";
  if (code === "unknown") return "";
  return sceneLabels[code] ?? code;
}

function FilenameCell({ item }: { item: CorpusTaskItem }): React.JSX.Element {
  const [copied, setCopied] = useState(false);
  const fullName = item.rel_path || item.filename;
  const copy = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(fullName);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };
  return (
    <span className={styles.filenameCell}>
      <span
        className={styles.filenameText ?? ""}
        title={`${fullName}\nTask ${item.task_id}`}
        data-testid="corpus-filename"
      >
        {item.filename}
      </span>
      <Button
        size="small"
        onClick={copy}
        title={`Copy full path: ${fullName}`}
        aria-label={`Copy full path of ${item.filename}`}
        data-testid="corpus-copy-filename"
      >
        {copied ? "Copied" : "Copy"}
      </Button>
    </span>
  );
}

export function CorpusTable({
  items,
  sceneLabels,
  hasMore = false,
  loadingMore = false,
  onLoadMore,
  appendError = null,
  onRetryAppend,
  onViewTask,
  onRevoke,
}: CorpusTableProps): React.JSX.Element {
  const columns: TableColumnsType<CorpusTaskItem> = [
    {
      title: "Audio file",
      key: "file",
      width: 280,
      render: (_value, item) => <FilenameCell item={item} />,
    },
    {
      title: "Submitter / Assignee",
      key: "annotator",
      width: 180,
      render: (_value, item) => corpusAnnotatorName(item),
    },
    {
      title: "Status",
      key: "status",
      width: 130,
      render: (_value, item) => {
        const title =
          `task_status=${item.task_status}` +
          (item.pool_state ? ` · pool_state=${item.pool_state}` : "");
        return (
          <span title={title}>
            <Status value={item.status} />
          </span>
        );
      },
    },
    {
      title: "Duration",
      key: "duration",
      width: 120,
      align: "right",
      render: (_value, item) => (
        <span className={styles.numericCell}>
          {formatCorpusDuration(item.duration)}
        </span>
      ),
    },
    {
      title: "Source scenes",
      key: "source_scenes",
      width: 180,
      render: (_value, item) =>
        (item.source_scenes ?? [])
          .map((code) => sceneLabel(code, sceneLabels))
          .filter(Boolean)
          .join(", ") || "—",
    },
    {
      title: "Confidence",
      key: "confidence",
      width: 120,
      render: (_value, item) =>
        CONFIDENCE_LABELS[item.source_confidence ?? "unknown"] ??
        item.source_confidence ??
        "Unknown",
    },
    {
      title: "Review",
      key: "review",
      width: 220,
      render: (_value, item) => {
        const review = item.review_status ?? "pending";
        const human = (item.human_scenes ?? [])
          .map((code) => sceneLabel(code, sceneLabels))
          .filter(Boolean);
        return `${REVIEW_STATUS_LABELS[review] ?? review}${human.length ? ` · ${human.join(", ")}` : ""}`;
      },
    },
    {
      title: "Updated",
      key: "updated",
      width: 170,
      className: "admin-table-meta",
      render: (_value, item) =>
        formatCorpusDateTime(item.submitted_at ?? item.updated_at),
    },
  ];

  if (onViewTask)
    columns.push({
      title: "Actions",
      key: "actions",
      width: 160,
      fixed: "right",
      align: "left",
      render: (_value, item) => (
        <div className="admin-row-actions">
          <Button size="small" onClick={() => onViewTask(item.task_id)}>
            View
          </Button>
          {item.current_version_id && onRevoke && (
            <Button size="small" danger onClick={() => onRevoke(item)}>
              Revoke
            </Button>
          )}
        </div>
      ),
    });

  return (
    <>
      <AdminTable<CorpusTaskItem>
        columns={columns}
        dataSource={items}
        rowKey="task_id"
        bodyTestId="corpus-tbody"
      />
      <AdminListFooter>
        <span>
          {items.length} tasks{hasMore ? " loaded" : ""}
        </span>
        {appendError === null ? (
          hasMore && (
            <Button
              loading={loadingMore}
              onClick={() => onLoadMore?.()}
              data-testid="corpus-load-more"
            >
              Load more
            </Button>
          )
        ) : (
          <div
            className={styles.appendError}
            role="alert"
            data-testid="corpus-append-error"
          >
            <span>{appendError}</span>
            <Button
              onClick={() => onRetryAppend?.()}
              data-testid="corpus-append-retry"
            >
              Retry loading more
            </Button>
          </div>
        )}
      </AdminListFooter>
    </>
  );
}
