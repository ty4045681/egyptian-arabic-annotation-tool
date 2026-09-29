import { Alert, Button, Card, Empty, Select, Skeleton, Tag } from "antd";
import type { ReactNode } from "react";
import { facetsSchema, type Timeseries } from "./schemas";
import { dateTime, label, messageFor, useAdminQuery } from "./api";

export function QueryState({
  query,
  children,
}: {
  query: {
    isPending: boolean;
    isError: boolean;
    error: unknown;
    data?: unknown;
    refetch: () => unknown;
  };
  children: ReactNode;
}): React.JSX.Element {
  if (query.isPending)
    return (
      <Card>
        <Skeleton active paragraph={{ rows: 4 }} />
      </Card>
    );
  if (query.isError)
    return (
      <>
        <Alert
          type="error"
          showIcon
          title={messageFor(query.error)}
          action={<Button onClick={() => void query.refetch()}>Retry</Button>}
        />
        {query.data !== undefined && children}
      </>
    );
  return <>{children}</>;
}

export function WriteError({
  error,
  frozen,
}: {
  error: unknown;
  frozen: boolean;
}): React.JSX.Element | null {
  return error ? (
    <Alert
      role="alert"
      showIcon
      type="error"
      title={messageFor(error)}
      description={
        frozen
          ? "The result is not yet confirmed. Retry will send the same request. Keep this window open."
          : undefined
      }
    />
  ) : null;
}

export function PageTitle({
  title,
  description,
  actions,
}: {
  title: string;
  description: string;
  actions?: ReactNode;
}): React.JSX.Element {
  return (
    <div className="admin-page-heading">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions}
    </div>
  );
}

export function Metrics({
  items,
}: {
  items: { label: string; value: ReactNode; hint?: ReactNode }[];
}): React.JSX.Element {
  return (
    <div className="admin-metrics">
      {items.map((item) => (
        <Card key={item.label} size="small">
          <span className="admin-muted">{item.label}</span>
          <strong className="admin-metric-value">{item.value}</strong>
          {item.hint && <small className="admin-muted">{item.hint}</small>}
        </Card>
      ))}
    </div>
  );
}

export function Status({ value }: { value: string }): React.JSX.Element {
  const color = [
    "annotated",
    "active",
    "passed",
    "confirmed",
    "completed",
  ].includes(value)
    ? "green"
    : ["awaiting_review", "deactivated", "revoked", "invalidated"].includes(
          value,
        )
      ? "red"
      : ["pending", "assigned", "in_progress"].includes(value)
        ? "gold"
        : "default";
  return <Tag color={color}>{label(value)}</Tag>;
}

export function SceneSelect({
  value,
  onChange,
  multiple = false,
  disabled = false,
  id,
}: {
  value: string[];
  onChange: (value: string[]) => void;
  multiple?: boolean;
  disabled?: boolean;
  id?: string;
}): React.JSX.Element {
  const catalog = useAdminQuery("/api/admin/metadata/facets", facetsSchema);
  return (
    <>
      <Select
        {...(id ? { id } : {})}
        aria-label="Scenes"
        showSearch={{ optionFilterProp: "label" }}
        {...(multiple ? { mode: "multiple" } : {})}
        allowClear
        className="admin-full-width"
        value={multiple ? value : value[0]}
        loading={catalog.isPending}
        disabled={disabled || !catalog.data || catalog.isError}
        onChange={(next: string | string[] | undefined) =>
          onChange(
            next === undefined ? [] : Array.isArray(next) ? next : [next],
          )
        }
        options={
          catalog.data?.scenes.map((scene) => ({
            value: scene.code,
            label: scene.label_en,
          })) ?? []
        }
      />
      {catalog.isError && (
        <Alert
          type="error"
          title="Scene catalog unavailable"
          action={
            <Button onClick={() => void catalog.refetch()}>
              Retry catalog
            </Button>
          }
        />
      )}
    </>
  );
}

export function ActivityChart({
  data,
}: {
  data: Timeseries;
}): React.JSX.Element {
  const maximum = Math.max(
    1,
    ...data.items.map(
      (point) => point.annotated + point.skipped + point.revoked,
    ),
  );
  return (
    <>
      {data.items.length ? (
        <div
          className="admin-chart"
          role="img"
          aria-label="Annotated, skipped and revoked activity"
        >
          {data.items.map((point) => (
            <div
              key={point.period}
              className="admin-chart-column"
              title={`${dateTime(point.period)}: ${point.annotated} annotated, ${point.skipped} skipped, ${point.revoked} revoked`}
            >
              <div className="admin-chart-stack">
                {(["revoked", "skipped", "annotated"] as const).map((kind) => (
                  <span
                    key={kind}
                    className={`admin-bar-${kind}`}
                    style={{ height: `${(point[kind] / maximum) * 100}%` }}
                  />
                ))}
              </div>
              <small>
                {new Date(point.period).toLocaleDateString(undefined, {
                  month: "short",
                  day: "numeric",
                })}
              </small>
            </div>
          ))}
        </div>
      ) : (
        <Empty description="No annotation activity in this period." />
      )}
      <div className="admin-chart-legend">
        {(["annotated", "skipped", "revoked"] as const).map((kind) => (
          <span key={kind}>
            <i className={`admin-bar-${kind}`} />
            {label(kind)}{" "}
            <b>
              {data.items
                .reduce((sum, item) => sum + item[kind], 0)
                .toLocaleString()}
            </b>
          </span>
        ))}
      </div>
    </>
  );
}
