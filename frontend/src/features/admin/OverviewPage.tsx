import { metadataChips, metadataKeys } from "./metadataFilterModel";
import { useUrlFilters } from "./useUrlFilters";
import { AdminList, AdminListFooter } from "./AdminList";
import { dateChip } from "./filterModel";
import { Alert, Button, Card, Empty, Progress } from "antd";
import { useSearchParams } from "react-router-dom";
import { ActivityChart, Metrics, PageTitle, QueryState } from "./AdminParts";
import { dateTime, duration, label, useAdminQuery, type Filters } from "./api";
import { dashboardDates } from "./dateFilters";
import { facetsSchema, overviewSchema, timeseriesSchema } from "./schemas";
import { AdminFilterBar, DateRangeFilter } from "./AdminFilters";
import { MetadataFilters } from "./MetadataFilters";

export function OverviewPage(): React.JSX.Element {
  const [search, setSearch] = useSearchParams();
  const {
    values: metadata,
    patch,
    reset,
    dates,
    setDates,
  } = useUrlFilters(["q", ...metadataKeys]);
  const filters = { ...dashboardDates(search), ...metadata };
  const overview = useAdminQuery(
    "/api/admin/overview",
    overviewSchema,
    filters,
  );
  const series = useAdminQuery(
    "/api/admin/timeseries",
    timeseriesSchema,
    filters,
  );
  const facets = useAdminQuery("/api/admin/metadata/facets", facetsSchema);
  const data = overview.data;
  const total = data?.totals.total_audio_count ?? 0;
  const completion =
    total && data
      ? ((data.totals.annotated_count + data.totals.skipped_count) / total) *
        100
      : 0;
  const go = (view: string, extra: Filters = {}) =>
    setSearch({ view, ...extra });
  const options = {
    scenes:
      facets.data?.scenes.map((scene) => ({
        value: scene.code,
        label: scene.label_en,
      })) ?? [],
    batches:
      facets.data?.batches.map((batch) => ({
        value: batch.batch_code,
        label: batch.batch_code,
      })) ?? [],
  };
  const chips = [
    ...dateChip(dates, () => setDates({ range: "all", from: "", to: "" })),
    ...(metadata.q
      ? [
          {
            key: "q",
            label: `Search: ${metadata.q}`,
            onRemove: () => patch({ q: "" }),
          },
        ]
      : []),
    ...metadataChips(metadata, options, patch),
  ];
  return (
    <div className="admin-stack" id="overviewView">
      <PageTitle
        title="Overview"
        description="Corpus health and annotation throughput"
        actions={
          <span className="admin-muted">
            Updated {dateTime(data?.updated_at)}
          </span>
        }
      />
      <AdminFilterBar
        label="Filter overview"
        search={{
          value: metadata.q ?? "",
          onChange: (q) => patch({ q }, true),
          label: "Search corpus snapshot",
          placeholder: "Search filename or path…",
        }}
        primary={<DateRangeFilter value={dates} onChange={setDates} />}
        chips={chips}
        onReset={reset}
        summary={
          overview.isFetching ? (
            "Updating snapshot…"
          ) : overview.isError ? (
            "Snapshot unavailable"
          ) : (
            <>
              <strong>{total.toLocaleString()}</strong> tasks in corpus snapshot
            </>
          )
        }
      >
        <MetadataFilters
          values={metadata}
          options={options}
          apply={patch}
          disabled={!facets.data || facets.isError}
        />
        <p className="af-note">
          Date range applies to activity statistics. Corpus totals reflect the
          current snapshot.
        </p>
        {facets.isError && (
          <Alert
            type="error"
            title="Filter options unavailable"
            action={
              <Button onClick={() => void facets.refetch()}>
                Retry catalog
              </Button>
            }
          />
        )}
      </AdminFilterBar>
      <QueryState query={overview}>
        {data && (
          <>
            <div className="admin-alerts" id="overviewAlerts">
              {data.cross_check.pending_review_count > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  title={`${data.cross_check.pending_review_count} cross-check rounds awaiting review`}
                  description={`${duration(data.cross_check.blocked_audio_seconds)} held from training export`}
                  action={
                    <Button onClick={() => go("cross-checks")}>
                      Review queue
                    </Button>
                  }
                />
              )}
              {data.queue_health.stale_assignments > 0 && (
                <Alert
                  type="warning"
                  showIcon
                  title={`${data.queue_health.stale_assignments} long-held assignments`}
                  action={
                    <Button
                      onClick={() =>
                        go("quality", { signal: "stale_assignment" })
                      }
                    >
                      Inspect
                    </Button>
                  }
                />
              )}
            </div>
            <Metrics
              items={[
                {
                  label: "Total audio",
                  value: (
                    <span id="kpiTotalAudio">{total.toLocaleString()}</span>
                  ),
                  hint: `${duration(data.totals.total_audio_duration_seconds)} duration`,
                },
                {
                  label: "Annotated",
                  value: data.totals.annotated_count.toLocaleString(),
                  hint: `${duration(data.totals.annotated_duration_seconds)} published audio`,
                },
                {
                  label: "Pending",
                  value: data.totals.pending_count.toLocaleString(),
                  hint: `${data.pending.available_count} available to claim`,
                },
                {
                  label: "Skipped",
                  value: data.totals.skipped_count.toLocaleString(),
                  hint: `${duration(data.totals.skipped_duration_seconds)} audio duration`,
                },
                {
                  label: "Trainable segments",
                  value: data.segments.trainable_count.toLocaleString(),
                  hint: `${duration(data.segments.trainable_duration_seconds)} clean duration`,
                },
                {
                  label: "Active annotators",
                  value: data.activity.active_annotators.toLocaleString(),
                  hint: `${data.activity.daily_throughput_7d.toFixed(1)} tasks / day (7d)`,
                },
              ]}
            />
            <Card className="admin-activity-card" title="Annotation activity">
              <p className="admin-muted">
                Submissions per {filters.bucket === "week" ? "week" : "day"}
              </p>
              <QueryState query={series}>
                {series.data && <ActivityChart data={series.data} />}
              </QueryState>
            </Card>
            <div className="admin-grid">
              <AdminList title="Completion">
                <div className="admin-list-summary">
                  <strong className="admin-metric-value">
                    {completion.toFixed(1)}%
                  </strong>
                  <p className="admin-list-meta">of all audio resolved</p>
                  <Progress percent={completion} showInfo={false} />
                </div>
                <dl className="admin-summary-list">
                  <div className="admin-list-item">
                    <dt>Annotated</dt>
                    <dd>{data.totals.annotated_count}</dd>
                  </div>
                  <div className="admin-list-item">
                    <dt>Skipped</dt>
                    <dd>{data.totals.skipped_count}</dd>
                  </div>
                  <div className="admin-list-item">
                    <dt>Pending</dt>
                    <dd>{data.totals.pending_count}</dd>
                  </div>
                  <div className="admin-list-item">
                    <dt>Oldest pending</dt>
                    <dd className="admin-list-meta">
                      {dateTime(data.pending.oldest_created_at)}
                    </dd>
                  </div>
                </dl>
              </AdminList>
              <AdminList
                title="Queue health"
                extra={
                  <span className="admin-muted">
                    <span id="queueTotal">{data.totals.pending_count}</span>{" "}
                    pending
                  </span>
                }
              >
                <Distribution
                  rows={(
                    ["available", "assigned", "reserved", "ineligible"] as const
                  ).map((key) => ({
                    name: label(key),
                    value: data.pending[`${key}_count`],
                  }))}
                />
                <AdminListFooter>
                  <span>Queue groups may overlap.</span>
                  <Button
                    onClick={() =>
                      go("quality", { signal: "stale_assignment" })
                    }
                  >
                    {data.queue_health.stale_assignments} long-held assignments
                  </Button>
                </AdminListFooter>
              </AdminList>
              <AdminList title="Cross-checks">
                <div className="admin-list-summary">
                  <strong className="admin-metric-value">
                    {data.cross_check.pending_review_count}
                  </strong>
                  <p className="admin-list-meta">
                    awaiting administrator review
                  </p>
                </div>
                <dl className="admin-summary-list">
                  <div className="admin-list-item">
                    <dt>In progress</dt>
                    <dd>{data.cross_check.in_progress_count}</dd>
                  </div>
                  <div className="admin-list-item">
                    <dt>Passed</dt>
                    <dd>{data.cross_check.passed_count}</dd>
                  </div>
                  <div className="admin-list-item">
                    <dt>Training audio on hold</dt>
                    <dd>{duration(data.cross_check.blocked_audio_seconds)}</dd>
                  </div>
                </dl>
                <AdminListFooter>
                  <Button onClick={() => go("cross-checks")}>
                    Open review queue
                  </Button>
                </AdminListFooter>
              </AdminList>
            </div>
            <div className="admin-grid-two">
              <AdminList title="Source scenes">
                <Distribution
                  rows={data.source_scenes.map((item) => ({
                    name: item.label,
                    value: item.task_count,
                    detail: duration(item.duration_seconds),
                  }))}
                />
                <AdminListFooter>Scene groups may overlap.</AdminListFooter>
              </AdminList>
              <AdminList title="Source confidence">
                <Distribution
                  rows={data.confidence_buckets.map((item) => ({
                    name: label(item.confidence),
                    value: item.task_count,
                  }))}
                />
              </AdminList>
              <AdminList title="Source batches">
                <Distribution
                  rows={data.source_batches.map((item) => ({
                    name: item.batch_code,
                    value: item.task_count,
                  }))}
                />
              </AdminList>
              <AdminList title="Human scene review">
                <Distribution
                  rows={data.review_statuses.map((item) => ({
                    name: label(item.status),
                    value: item.task_count,
                  }))}
                />
              </AdminList>
              <AdminList title="Categories">
                <Distribution
                  rows={data.categories.map((item) => ({
                    name: item.category,
                    value: item.count,
                  }))}
                />
              </AdminList>
              <AdminList title="Skip reasons">
                <Distribution
                  rows={data.skip_reasons.map((item) => ({
                    name: label(item.reason),
                    value: item.count,
                  }))}
                />
              </AdminList>
            </div>
          </>
        )}
      </QueryState>
    </div>
  );
}

function Distribution({
  rows,
}: {
  rows: { name: string; value: number; detail?: string }[];
}): React.JSX.Element {
  const maximum = Math.max(1, ...rows.map((row) => row.value));
  return rows.length ? (
    <ul className="admin-list-items admin-distribution">
      {rows.map((row) => (
        <li key={row.name} className="admin-list-item">
          <div className="admin-distribution-label">
            <span>{row.name}</span>
            <span className="admin-distribution-value">
              <span>{row.value.toLocaleString()}</span>
              {row.detail && (
                <span className="admin-list-meta">{row.detail}</span>
              )}
            </span>
          </div>
          <meter
            min={0}
            max={maximum}
            value={row.value}
            aria-label={row.name}
          />
        </li>
      ))}
    </ul>
  ) : (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description="No distribution data yet."
    />
  );
}
