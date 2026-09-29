import { useUrlFilters } from "./useUrlFilters";
import { dateChip } from "./filterModel";
import { Alert, Button, Select } from "antd";
import { useSearchParams } from "react-router-dom";
import { AdminFilterBar, DateRangeFilter, FilterChoices } from "./AdminFilters";
import { dashboardDates } from "./dateFilters";
import { Metrics, PageTitle, QueryState, Status } from "./AdminParts";
import { AdminList, AdminListFooter, AdminTable } from "./AdminList";
import {
  dateTime,
  downloadCsv,
  duration,
  label,
  useAdmin,
  useAdminPages,
  useAdminQuery,
  useAnnotatorDirectory,
} from "./api";
import { auditSchema, qualitySchema } from "./schemas";

export function QualityPage(): React.JSX.Element {
  const [search, setSearch] = useSearchParams();
  const { values, patch, reset, dates, setDates } = useUrlFilters([
    "q",
    "signal",
    "annotator_id",
  ]);
  const query = useAdminQuery("/api/admin/quality", qualitySchema, {
    ...dashboardDates(search),
    ...values,
  });
  const people = useAnnotatorDirectory();
  const { openTask, action } = useAdmin();
  const data = query.data;
  const personOptions =
    people.data?.pages
      .flatMap((page) => page.items)
      .map((person) => ({ value: person.id, label: person.username })) ?? [];
  const signalOptions = [
    {
      value: "",
      label: "All",
      count: data
        ? data.stats.unusually_fast + data.stats.stale_assignments
        : undefined,
    },
    {
      value: "unusually_fast",
      label: "Unusually fast",
      count: data?.stats.unusually_fast,
      color: "#df9914",
    },
    {
      value: "stale_assignment",
      label: "Long-held assignment",
      count: data?.stats.stale_assignments,
      color: "#d86161",
    },
  ];
  const matched = signalOptions.find(
    (item) => item.value === (values.signal ?? ""),
  )?.count;
  const chips = [
    ...dateChip(dates, () => setDates({ range: "all", from: "", to: "" })),
    ...(values.q
      ? [
          {
            key: "q",
            label: `Search: ${values.q}`,
            onRemove: () => patch({ q: "" }),
          },
        ]
      : []),
    ...(values.signal
      ? [
          {
            key: "signal",
            label:
              signalOptions.find((item) => item.value === values.signal)
                ?.label ?? values.signal,
            onRemove: () => patch({ signal: "" }),
          },
        ]
      : []),
    ...(values.annotator_id
      ? [
          {
            key: "annotator",
            label: `Annotator: ${personOptions.find((item) => item.value === values.annotator_id)?.label ?? values.annotator_id}`,
            onRemove: () => patch({ annotator_id: "" }),
          },
        ]
      : []),
  ];
  return (
    <div className="admin-stack" id="qualityView">
      <PageTitle
        title="Review & quality"
        description="Signals for human review. These are not automatic quality scores."
      />
      <QueryState query={query}>
        {data && (
          <Metrics
            items={[
              { label: "Unusually fast", value: data.stats.unusually_fast },
              {
                label: "Long-held assignments",
                value: data.stats.stale_assignments,
              },
              {
                label: "Excluded segments",
                value: `${(data.stats.excluded_segment_rate * 100).toFixed(1)}%`,
                hint: `${data.stats.excluded_segment_count} segments`,
              },
              { label: "Revoked", value: data.stats.revoked_count },
            ]}
          />
        )}
      </QueryState>
      <AdminFilterBar
        label="Filter quality signals"
        search={{
          value: values.q ?? "",
          onChange: (q) => patch({ q }, true),
          label: "Search quality signals",
          placeholder: "Search filename or path…",
        }}
        primary={
          <>
            <DateRangeFilter value={dates} onChange={setDates} />
            <Select
              aria-label="Annotator"
              allowClear
              showSearch={{ optionFilterProp: "label" }}
              placeholder="All annotators"
              value={values.annotator_id || undefined}
              onChange={(value: string | undefined) =>
                patch({ annotator_id: value ?? "" })
              }
              options={personOptions}
              loading={people.isFetching}
              disabled={!people.data || people.isError}
            />
          </>
        }
        onReset={reset}
        chips={chips}
        summary={
          query.isFetching ? (
            "Updating signals…"
          ) : query.isError ? (
            "Signals unavailable"
          ) : (
            <>
              <strong>{data?.items.length ?? 0}</strong> of {matched ?? 0}{" "}
              signals shown
            </>
          )
        }
        exportDisabled={
          !data?.items.length || query.isFetching || query.isError
        }
        onExport={() =>
          downloadCsv(
            "quality-signals.csv",
            [
              "filename",
              "annotator",
              "signal",
              "submitted_at",
              "elapsed_seconds",
              "last_active",
            ],
            (data?.items ?? []).map((row) => [
              row.filename,
              row.username,
              row.type,
              row.created_at,
              row.elapsed_seconds,
              row.last_activity_at,
            ]),
          )
        }
      >
        <div className="af-row">
          <FilterChoices
            label="Signal"
            value={values.signal ?? ""}
            options={signalOptions}
            onChange={(signal) => patch({ signal })}
          />
        </div>
        {people.isError && (
          <Alert
            type="error"
            title="Annotator options unavailable"
            action={
              <Button onClick={() => void people.refetch()}>
                Retry annotators
              </Button>
            }
          />
        )}
      </AdminFilterBar>
      <AdminList aria-label="Quality signals">
        <QueryState query={query}>
          <AdminTable
            dataSource={data?.items ?? []}
            rowKey={(row) => `${row.task_id}:${row.type}`}
            scroll={{ x: 840 }}
            columns={[
              {
                title: "Audio",
                render: (_, row) => (
                  <span className="admin-filename">{row.filename}</span>
                ),
              },
              { title: "Annotator", dataIndex: "username" },
              { title: "Signal", render: (_, row) => label(row.type) },
              {
                title: "Value",
                render: (_, row) =>
                  row.elapsed_seconds == null
                    ? `Last active ${dateTime(row.last_activity_at)}`
                    : duration(row.elapsed_seconds),
              },
              {
                title: "Submitted",
                className: "admin-table-meta",
                render: (_, row) => dateTime(row.created_at),
              },
              {
                title: "Actions",
                align: "left",
                render: (_, row) => (
                  <div className="admin-row-actions">
                    <Button size="small" onClick={() => openTask(row.task_id)}>
                      View
                    </Button>
                    {row.type === "stale_assignment" && (
                      <Button
                        size="small"
                        danger
                        onClick={() =>
                          action({
                            kind: "release",
                            taskId: row.task_id,
                            filename: row.filename,
                          })
                        }
                      >
                        Release
                      </Button>
                    )}
                  </div>
                ),
              },
            ]}
          />
        </QueryState>
        <AdminListFooter>
          <span>
            {query.isPending
              ? "Loading signals…"
              : `${data?.items.length ?? 0} signals shown`}
          </span>
        </AdminListFooter>
      </AdminList>
      <Button onClick={() => setSearch({ view: "cross-checks" })}>
        Open cross-check review queue
      </Button>
    </div>
  );
}

export function ActivityPage(): React.JSX.Element {
  const [search] = useSearchParams();
  const { values, patch, reset, dates, setDates } = useUrlFilters([
    "q",
    "action_type",
  ]);
  const query = useAdminPages("/api/admin/audit", auditSchema, {
    ...dashboardDates(search),
    ...values,
  });
  const rows = query.data?.pages.flatMap((page) => page.items) ?? [];
  const actionOptions = [
    { value: "", label: "All actions" },
    ...[
      "revoke_annotations",
      "restore_annotations",
      "deactivate_annotator",
      "release_assignment",
      "set_scene_scope",
      "cross_check_decision",
      "cross_check_cancel",
      "update_cross_check_settings",
      "admin_auth_success",
      "admin_auth_failure",
    ].map((value) => ({ value, label: label(value) })),
  ];
  const chips = [
    ...dateChip(dates, () => setDates({ range: "all", from: "", to: "" })),
    ...(values.q
      ? [
          {
            key: "q",
            label: `Search: ${values.q}`,
            onRemove: () => patch({ q: "" }),
          },
        ]
      : []),
    ...(values.action_type
      ? [
          {
            key: "action",
            label: label(values.action_type),
            onRemove: () => patch({ action_type: "" }),
          },
        ]
      : []),
  ];
  return (
    <div className="admin-stack" id="activityView">
      <PageTitle
        title="Activity log"
        description="Security-sensitive and data-changing administrator actions."
      />
      <AdminFilterBar
        label="Filter activity"
        search={{
          value: values.q ?? "",
          onChange: (q) => patch({ q }, true),
          label: "Search activity",
          placeholder: "Search reason, action or administrator…",
        }}
        primary={
          <>
            <DateRangeFilter value={dates} onChange={setDates} />
            <Select
              aria-label="Action type"
              showSearch={{ optionFilterProp: "label" }}
              virtual={false}
              value={values.action_type ?? ""}
              options={actionOptions}
              onChange={(action_type: string) => patch({ action_type })}
            />
          </>
        }
        onReset={reset}
        chips={chips}
        summary={
          query.isFetching && !query.isFetchingNextPage ? (
            "Updating activity…"
          ) : query.isError ? (
            "Activity unavailable"
          ) : (
            <>
              <strong>{rows.length}</strong> actions loaded
              {query.hasNextPage ? " · more available" : ""}
            </>
          )
        }
        exportDisabled={!rows.length || query.isFetching || query.isError}
        onExport={() =>
          downloadCsv(
            "admin-activity.csv",
            ["time", "action", "admin_key_id", "items", "reason", "status"],
            rows.map((row) => [
              row.created_at,
              row.action_type,
              row.key_id,
              row.item_count,
              row.reason,
              row.status,
            ]),
          )
        }
      />
      <AdminList aria-label="Administrator activity">
        <QueryState query={query}>
          <AdminTable
            dataSource={rows}
            rowKey="id"
            scroll={{ x: 900 }}
            columns={[
              {
                title: "Time",
                className: "admin-table-meta",
                render: (_, row) => dateTime(row.created_at),
              },
              { title: "Action", render: (_, row) => label(row.action_type) },
              { title: "Administrator", dataIndex: "key_id" },
              { title: "Items", dataIndex: "item_count", align: "right" },
              { title: "Reason", dataIndex: "reason", width: 280 },
              {
                title: "Status",
                render: (_, row) => <Status value={row.status} />,
              },
            ]}
          />
        </QueryState>
        <AdminListFooter>
          <span>
            {query.isPending
              ? "Loading activity…"
              : `${rows.length} events${query.hasNextPage ? " loaded" : ""}`}
          </span>
          {query.hasNextPage && (
            <Button
              onClick={() => void query.fetchNextPage()}
              loading={query.isFetchingNextPage}
            >
              Load more
            </Button>
          )}
        </AdminListFooter>
      </AdminList>
    </div>
  );
}
