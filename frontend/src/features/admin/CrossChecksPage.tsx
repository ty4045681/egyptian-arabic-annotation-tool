import { filterToday } from "./filterModel";
import { useState } from "react";
import { Alert, App, Button, Tabs } from "antd";
import { ControlOutlined } from "@ant-design/icons";
import { useSearchParams } from "react-router-dom";
import { PageTitle, QueryState, Status } from "./AdminParts";
import { AdminList, AdminListFooter, AdminTable } from "./AdminList";
import {
  apiPath,
  dateTime,
  useAdmin,
  duration,
  downloadCsv,
  label,
  useAdminPages,
  useAdminQuery,
  useAnnotatorDirectory,
  type Filters,
} from "./api";
import {
  crossRoundSchema,
  crossStateSchema,
  facetsSchema,
  overviewSchema,
  pageSchema,
  settingsSchema,
} from "./schemas";
import { decodeCorpusUrlSearch, shiftDateString } from "./corpus/queryCodec";
import { CrossCheckReview } from "./CrossCheckReview";
import { SamplingSettings } from "./SamplingSettings";
import { CrossCheckFilters } from "./CrossCheckFilters";
import { crossCheckFilterKeys, crossCheckReasons } from "./crossCheckModel";
import "./cross-checks.css";

export function CrossChecksPage(): React.JSX.Element {
  const [search, setSearch] = useSearchParams();
  const [settings, setSettings] = useState(false);
  const [autoNext, setAutoNext] = useState(false);
  const { client } = useAdmin();
  const { message } = App.useApp();
  const state = crossStateSchema.safeParse(
    search.get("cc_state") ?? "awaiting_review",
  );
  const activeState =
    search.get("cc_state") === "all"
      ? "all"
      : state.success
        ? state.data
        : "awaiting_review";
  const filters: Filters = Object.fromEntries(
    crossCheckFilterKeys.map((key) => [key, search.get(`cc_${key}`) ?? ""]),
  );
  const searchText = filters.q ?? "";
  const queryText = searchText;
  const dates = decodeCorpusUrlSearch(
    new URLSearchParams({
      range:
        search.get("cc_range") ??
        (filters.from || filters.to ? "custom" : "all"),
      from: filters.from ?? "",
      to: filters.to ?? "",
    }).toString(),
    filterToday(),
  );
  const query = useAdminPages("/api/admin/cross-checks", crossRoundSchema, {
    ...filters,
    q: queryText,
    state: activeState,
    timezone: "Asia/Shanghai",
    from: dates.from,
    to: dates.to ? shiftDateString(dates.to, 1) : "",
  });
  const summary = useAdminQuery("/api/admin/overview", overviewSchema);
  const sampling = useAdminQuery(
    "/api/admin/cross-check-settings",
    settingsSchema,
  );
  const facets = useAdminQuery("/api/admin/metadata/facets", facetsSchema);
  const people = useAnnotatorDirectory();
  const names = new Map(
    people.data?.pages
      .flatMap((page) => page.items)
      .map((person) => [person.id, person.username]),
  );
  const personName = (id: string | null) =>
    id ? (names.get(id) ?? id.slice(0, 8)) : "—";
  const personOptions = Array.from(names).map(([value, name]) => ({
    value,
    label: name,
  }));
  const roundId = search.get("round");
  const rows = query.data?.pages.flatMap((page) => page.items) ?? [];
  const cc = summary.data?.cross_check;
  const page = query.data?.pages[0];
  const counts = page?.state_counts;
  const comparedCount = cc
    ? cc.passed_count + cc.adjudicated_count + cc.pending_review_count
    : 0;
  function update(key: string, value: string) {
    setSearch((old) => {
      const next = new URLSearchParams(old);
      next.set(key, value);
      if (key !== "round") next.delete("round");
      return next;
    });
  }
  return (
    <div className="admin-stack cc-page" id="crossChecksView">
      <PageTitle
        title="Cross-checks"
        description="Independent second transcripts and administrator review. Dates are round creation time."
        actions={
          <div className="cc-heading-actions">
            {sampling.data && !sampling.isError && (
              <span
                className={`cc-badge ${sampling.data.enabled ? "cc-badge-success" : ""}`}
              >
                {sampling.data.enabled
                  ? `Sampling ${sampling.data.sampling_rate_bps / 100}%`
                  : "Sampling off"}
              </span>
            )}
            <Button
              id="ccSamplingButton"
              icon={<ControlOutlined />}
              onClick={() => setSettings(true)}
            >
              Sampling settings
            </Button>
          </div>
        }
      />
      <>
        <QueryState query={summary}>
          {cc && (
            <section
              className="cc-metrics"
              aria-label="Cross-check statistics"
              aria-describedby="ccSummaryScope"
            >
              <p id="ccSummaryScope" className="cc-sr-only">
                All-time, all scenes. Queue dates use round creation time.
              </p>
              {[
                {
                  label: "Awaiting review",
                  value: cc.pending_review_count,
                  hint: cc.oldest_pending_created_at
                    ? `Oldest ${dateTime(cc.oldest_pending_created_at)}`
                    : "No rounds waiting for review",
                },
                {
                  label: "Training export on hold",
                  value: duration(cc.blocked_audio_seconds),
                  hint: "Unique audio blocked by open rounds",
                },
                {
                  label: "Cross-check submissions",
                  value: summary.data?.totals.cross_check_submitted_count,
                  hint: `${duration(summary.data?.totals.cross_check_submitted_audio_seconds ?? 0)} labor · not published audio`,
                },
                {
                  label: "Pass rate",
                  value:
                    comparedCount && cc
                      ? `${Math.round((cc.passed_count / comparedCount) * 100)}%`
                      : "—",
                  hint: comparedCount
                    ? "Passed ÷ compared rounds"
                    : "No compared rounds yet",
                  title: "Passed ÷ (passed + awaiting review + adjudicated)",
                },
              ].map((metric) => (
                <div
                  className="cc-metric"
                  key={metric.label}
                  title={metric.title}
                >
                  <span className="cc-metric-label">{metric.label}</span>
                  <strong>{metric.value}</strong>
                  <small>{metric.hint}</small>
                </div>
              ))}
            </section>
          )}
        </QueryState>
        <CrossCheckFilters
          filters={filters}
          facets={facets}
          personOptions={personOptions}
          people={people}
          dates={dates}
          activeState={activeState}
          summary={
            query.isFetching && !query.isFetchingNextPage ? (
              "Updating rounds…"
            ) : query.isError ? (
              "Rounds unavailable"
            ) : (
              <>
                <strong>{page?.matched_count ?? rows.length}</strong> rounds ·{" "}
                {duration(page?.matched_duration_seconds ?? 0)} audio
              </>
            )
          }
          exportDisabled={!rows.length || query.isFetching || query.isError}
          onExport={() =>
            downloadCsv(
              "cross-checks.csv",
              [
                "filename",
                "round_id",
                "status",
                "original_annotator",
                "cross_check_annotator",
                "duration",
                "created_at",
              ],
              rows.map((row) => [
                row.filename,
                row.round_id,
                row.state,
                personName(row.original_annotator_id),
                personName(row.secondary_annotator_id),
                row.duration_seconds,
                row.created_at,
              ]),
            )
          }
          states={
            <Tabs
              className="af-status-tabs"
              activeKey={activeState}
              onChange={(value) => update("cc_state", value)}
              items={["all", ...crossStateSchema.options].map((value) => ({
                key: value,
                label: (
                  <span
                    className="cc-tab-label"
                    title="Rounds matching the other filters"
                  >
                    {label(value)}{" "}
                    <span className="af-count" aria-hidden="true">
                      {counts
                        ? value === "all"
                          ? Object.values(counts).reduce(
                              (sum, count) => sum + count,
                              0,
                            )
                          : (counts[value] ?? 0)
                        : "…"}
                    </span>
                  </span>
                ),
              }))}
            />
          }
        />
        {activeState === "awaiting_review" && (
          <Alert
            className="cc-queue-notice"
            type="info"
            showIcon
            title={`${page?.matched_count ?? "…"} matching rounds awaiting review. Compare transcripts and resolve differences.`}
            action={
              <Button
                type="primary"
                disabled={!rows.length}
                onClick={() => {
                  const first = rows[0];
                  if (first) update("round", first.round_id);
                }}
              >
                Review next
              </Button>
            }
          />
        )}
        <AdminList
          className="cc-queue"
          aria-label={`${label(activeState)} rounds`}
        >
          <QueryState query={query}>
            <AdminTable
              className="cc-table"
              data-testid="cross-check-table"
              dataSource={rows}
              rowKey="round_id"
              scroll={{ x: 1080 }}
              locale={{
                emptyText: "No rounds match this state and filters.",
              }}
              columns={[
                {
                  title: "Audio",
                  width: 214,
                  render: (_, row) => (
                    <div className="cc-audio-cell">
                      <span className="cc-round-id" title={row.round_id}>
                        {row.round_id.slice(0, 8)}
                      </span>
                      <small title={row.filename ?? row.task_id}>
                        {row.filename ?? row.task_id.slice(0, 8)}
                      </small>
                      <small>
                        {facets.data?.scenes.find(
                          (scene) => scene.code === row.source_scene,
                        )?.label_en ??
                          row.source_scene ??
                          "Unknown scene"}
                      </small>
                    </div>
                  ),
                },
                {
                  title: "Original → Cross-check",
                  width: 210,
                  render: (_, row) => (
                    <span className="cc-annotator-pair">
                      <bdi>{personName(row.original_annotator_id)}</bdi>
                      <span className="admin-muted"> → </span>
                      <bdi>
                        {row.secondary_annotator_id
                          ? personName(row.secondary_annotator_id)
                          : "In progress"}
                      </bdi>
                    </span>
                  ),
                },
                {
                  title: "Duration",
                  width: 85,
                  align: "right",
                  render: (_, row) => duration(row.duration_seconds),
                },
                {
                  title: "State",
                  width: 126,
                  render: (_, row) => <Status value={row.state} />,
                },
                {
                  title: "Word difference",
                  width: 144,
                  render: (_, row) =>
                    row.word_difference_rate === null ? (
                      <span className="admin-muted">—</span>
                    ) : (
                      <span
                        className={`cc-difference ${row.word_difference_rate > 0.1 ? "cc-difference-high" : ""}`}
                      >
                        <span
                          className="cc-difference-track"
                          aria-hidden="true"
                        >
                          <span
                            style={{
                              width: `${Math.min(100, row.word_difference_rate * 200)}%`,
                            }}
                          />
                        </span>
                        <span>
                          {(row.word_difference_rate * 100).toFixed(1)}%
                        </span>
                      </span>
                    ),
                },
                {
                  title: "Reasons",
                  width: 230,
                  render: (_, row) =>
                    row.reason_codes.length ? (
                      <div className="cc-reasons">
                        {row.reason_codes.map((code) => {
                          const reason = crossCheckReasons.find(
                            (item) => item.value === code,
                          );
                          return (
                            <span
                              className="cc-badge cc-badge-warning"
                              key={code}
                              title={reason?.label ?? label(code)}
                            >
                              {reason?.shortLabel ??
                                reason?.label ??
                                label(code)}
                            </span>
                          );
                        })}
                      </div>
                    ) : (
                      <span className="admin-muted">—</span>
                    ),
                },
                {
                  title: "Created",
                  width: 120,
                  render: (_, row) => (
                    <time
                      className="cc-created"
                      dateTime={row.created_at}
                      title={dateTime(row.created_at)}
                    >
                      {new Date(row.created_at).toLocaleDateString(undefined, {
                        dateStyle: "medium",
                      })}
                    </time>
                  ),
                },
                {
                  title: <span className="cc-sr-only">Actions</span>,
                  width: 77,
                  align: "left",
                  render: (_, row) => (
                    <Button
                      data-cc-round={row.round_id}
                      size="small"
                      type={
                        row.state === "awaiting_review" ? "primary" : "default"
                      }
                      onClick={() => update("round", row.round_id)}
                    >
                      {row.state === "awaiting_review" ? "Review" : "View"}
                    </Button>
                  ),
                },
              ]}
            />
          </QueryState>
          <AdminListFooter>
            <span>
              {query.isPending
                ? "Loading rounds…"
                : `${rows.length} rounds${query.hasNextPage ? " loaded" : ""}`}{" "}
              · Training exports exclude audio under cross-check; audit exports
              may include it.
            </span>
            {query.hasNextPage && (
              <Button
                size="small"
                onClick={() => void query.fetchNextPage()}
                loading={query.isFetchingNextPage}
              >
                Load more
              </Button>
            )}
          </AdminListFooter>
        </AdminList>
      </>
      {roundId && (
        <CrossCheckReview
          key={roundId}
          roundId={roundId}
          autoNext={autoNext}
          onAutoNextChange={setAutoNext}
          personName={personName}
          onNext={async () => {
            try {
              const result = await client.read(
                apiPath("/api/admin/cross-checks", {
                  ...filters,
                  state: "awaiting_review",
                  timezone: "Asia/Shanghai",
                  from: dates.from,
                  to: dates.to ? shiftDateString(dates.to, 1) : "",
                  limit: "1",
                }),
                pageSchema(crossRoundSchema),
              );
              const next = result.items[0];
              if (next) update("round", next.round_id);
              else {
                setSearch((old) => {
                  const next = new URLSearchParams(old);
                  next.delete("round");
                  return next;
                });
                void message.success(
                  "No more awaiting rounds match these filters.",
                );
              }
            } catch {
              void message.error(
                "Decision saved. The next round could not be loaded; close the review and retry the queue.",
              );
            }
          }}
          onBack={() =>
            setSearch((old) => {
              const next = new URLSearchParams(old);
              next.delete("round");
              return next;
            })
          }
        />
      )}
      {settings && <SamplingSettings onClose={() => setSettings(false)} />}
    </div>
  );
}
