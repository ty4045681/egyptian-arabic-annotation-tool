import { DownloadOutlined } from "@ant-design/icons";
import { useUrlFilters } from "./useUrlFilters";
import { useState, type Key } from "react";
import {
  Alert,
  App,
  Avatar,
  Button,
  Card,
  Checkbox,
  Collapse,
  Drawer,
  Form,
  Input,
  Select,
  Space,
} from "antd";
import { AdminFilterBar, FilterChoices } from "./AdminFilters";
import { AdminList, AdminListFooter, AdminTable } from "./AdminList";
import { useSearchParams } from "react-router-dom";
import {
  ActivityChart,
  Metrics,
  PageTitle,
  QueryState,
  Status,
  WriteError,
} from "./AdminParts";
import {
  dateTime,
  downloadCsv,
  duration,
  useAdmin,
  useAdminPages,
  useAdminQuery,
  useAdminWrite,
  useAnnotatorDirectory,
} from "./api";
import {
  annotationSchema,
  annotatorDetailSchema,
  facetsSchema,
  timeseriesSchema,
  type Annotator,
  type AnnotatorDetail,
} from "./schemas";

function activity(person: Annotator): string {
  if (person.status !== "active") return "deactivated";
  if (!person.history.last_completed_at) return "never";
  return Date.now() - new Date(person.history.last_completed_at).getTime() <=
    7 * 86400000
    ? "recent"
    : "idle";
}

export function AnnotatorsPage(): React.JSX.Element {
  const [search] = useSearchParams();
  const query = useAnnotatorDirectory();
  const { values, patch, reset } = useUrlFilters(["q", "activity"], {
    activity: "all",
  });
  const q = values.q ?? "";
  const filter = values.activity ?? "all";
  const people = query.data?.pages.flatMap((page) => page.items) ?? [];
  const visible = people.filter(
    (person) =>
      person.username.toLocaleLowerCase().includes(q.toLocaleLowerCase()) &&
      (filter === "all" || activity(person) === filter),
  );
  const selected = search.get("annotator");
  const show = (id: string) => patch({ annotator: id });
  const options = [
    { value: "all", label: "All" },
    { value: "recent", label: "Active (7d)", color: "#22a06b" },
    { value: "idle", label: "Idle", color: "#df9914" },
    { value: "never", label: "Never active", color: "#94a3b8" },
    { value: "deactivated", label: "Deactivated", color: "#d86161" },
  ];
  const chips = [
    ...(q
      ? [{ key: "q", label: `Search: ${q}`, onRemove: () => patch({ q: "" }) }]
      : []),
    ...(filter !== "all"
      ? [
          {
            key: "activity",
            label:
              options.find((option) => option.value === filter)?.label ??
              filter,
            onRemove: () => patch({ activity: "" }),
          },
        ]
      : []),
  ];
  return (
    <div className="admin-stack" id="annotatorsView">
      <PageTitle
        title="Annotators"
        description="Manage contributors, access and annotation scope"
      />
      <AdminFilterBar
        label="Filter annotators"
        search={{
          value: q,
          onChange: (value) => patch({ q: value }, true),
          label: "Search annotators",
          id: "sidebarAnnotatorSearch",
          placeholder: "Search annotator name…",
        }}
        onReset={reset}
        chips={chips}
        summary={
          query.isFetching ? (
            "Loading annotators…"
          ) : query.isError ? (
            "Annotator directory incomplete"
          ) : (
            <>
              <strong>{visible.length}</strong> annotators
            </>
          )
        }
        exportDisabled={query.isFetching || query.isError || !visible.length}
        onExport={() =>
          downloadCsv(
            "annotators.csv",
            ["annotator", "current", "last_active", "status"],
            visible.map((person) => [
              person.username,
              person.current.annotated_count + person.current.skipped_count,
              person.history.last_completed_at,
              activity(person),
            ]),
          )
        }
      >
        <div className="af-row">
          <FilterChoices
            label="Activity"
            value={filter}
            options={options.map((option) => ({
              ...option,
              count:
                query.isFetching || query.isError
                  ? undefined
                  : people.filter(
                      (person) =>
                        person.username
                          .toLocaleLowerCase()
                          .includes(q.toLocaleLowerCase()) &&
                        (option.value === "all" ||
                          activity(person) === option.value),
                    ).length,
            }))}
            onChange={(value) => patch({ activity: value })}
          />
        </div>
      </AdminFilterBar>
      <AdminList aria-label="Annotators">
        <QueryState query={query}>
          <AdminTable<Annotator>
            data-testid="annotator-table"
            dataSource={visible}
            rowKey="id"
            scroll={{ x: 740 }}
            locale={{ emptyText: "No annotators match these filters." }}
            columns={[
              {
                title: "Annotator",
                key: "username",
                sorter: (a, b) => a.username.localeCompare(b.username),
                defaultSortOrder: "ascend",
                render: (_, person) => (
                  <Space>
                    <Avatar size={28}>
                      {person.username.slice(0, 2).toUpperCase()}
                    </Avatar>
                    <Button
                      className="admin-person-button"
                      data-annotator-id={person.id}
                      type="text"
                      aria-label={`View ${person.username}`}
                      onClick={() => show(person.id)}
                    >
                      {person.username}
                    </Button>
                  </Space>
                ),
              },
              {
                title: "Current annotations",
                key: "current",
                align: "right",
                sorter: (a, b) =>
                  a.current.annotated_count +
                  a.current.skipped_count -
                  b.current.annotated_count -
                  b.current.skipped_count,
                render: (_, person) =>
                  person.current.annotated_count + person.current.skipped_count,
              },
              {
                title: "Audio",
                align: "right",
                render: (_, person) =>
                  duration(person.current.duration_seconds),
              },
              {
                title: "Last active",
                className: "admin-table-meta",
                sorter: (a, b) =>
                  (a.history.last_completed_at ?? "").localeCompare(
                    b.history.last_completed_at ?? "",
                  ),
                render: (_, person) =>
                  dateTime(person.history.last_completed_at),
              },
              {
                title: "Status",
                render: (_, person) => <Status value={activity(person)} />,
              },
              {
                title: "Actions",
                align: "left",
                render: (_, person) => (
                  <Button size="small" onClick={() => show(person.id)}>
                    View
                  </Button>
                ),
              },
            ]}
          />
        </QueryState>
        <AdminListFooter>
          <span>
            {query.isPending
              ? "Loading annotators…"
              : `${visible.length} annotators${query.hasNextPage ? " loaded" : ""}`}
          </span>
          {query.hasNextPage && (
            <Button
              onClick={() => void query.fetchNextPage()}
              loading={query.isFetchingNextPage}
            >
              Load more annotators
            </Button>
          )}
        </AdminListFooter>
      </AdminList>
      {selected && (
        <AnnotatorDrawer
          key={selected}
          id={selected}
          onClose={() => patch({ annotator: "" })}
        />
      )}
    </div>
  );
}

function AnnotatorDrawer({
  id,
  onClose,
}: {
  id: string;
  onClose: () => void;
}): React.JSX.Element {
  const detail = useAdminQuery(
    `/api/admin/annotators/${encodeURIComponent(id)}`,
    annotatorDetailSchema,
  );
  const annotations = useAdminPages(
    `/api/admin/annotators/${encodeURIComponent(id)}/annotations`,
    annotationSchema,
    { lifecycle: "published" },
  );
  const series = useAdminQuery("/api/admin/timeseries", timeseriesSchema, {
    annotator_id: id,
  });
  const { action, openTask } = useAdmin();
  const [selection, setSelection] = useState<Key[]>([]);
  const data = detail.data;
  const rows = annotations.data?.pages.flatMap((page) => page.items) ?? [];
  const selected = rows.filter(
    (row) => selection.includes(row.version_id) && row.can_revoke,
  );
  const revoke = (items: typeof rows) =>
    action({
      kind: "revoke",
      items: items.map((row) => ({
        task_id: row.task_id,
        expected_version_id: row.version_id,
        filename: row.filename,
        annotator_id: id,
      })),
    });
  return (
    <Drawer
      open
      onClose={onClose}
      title={data?.username ?? "Annotator"}
      size={920}
      className="admin-drawer"
      rootClassName="admin-annotator-drawer"
      destroyOnHidden
      footer={
        <div className="admin-drawer-footer">
          <Button
            icon={<DownloadOutlined />}
            onClick={() =>
              downloadCsv(
                "annotations.csv",
                ["filename", "status", "duration", "submitted_at"],
                rows.map((row) => [
                  row.filename,
                  row.status,
                  row.duration,
                  row.submitted_at,
                ]),
              )
            }
          >
            Export visible CSV
          </Button>
          <Button
            id="deactivateAnnotatorButton"
            danger
            disabled={data?.status !== "active"}
            onClick={() => {
              if (data)
                action({
                  kind: "deactivate",
                  annotator: { id, username: data.username },
                });
            }}
          >
            Deactivate annotator
          </Button>
        </div>
      }
    >
      <QueryState query={detail}>
        {data && (
          <div className="admin-stack" id="annotatorDetail">
            <Space>
              <Avatar size={52}>
                {data.username.slice(0, 2).toUpperCase()}
              </Avatar>
              <div>
                <h2 id="annotatorName">{data.username}</h2>
                <Status value={data.status} />
              </div>
            </Space>
            <Metrics
              items={[
                {
                  label: "Current annotations",
                  value: (
                    <span id="annotatorCurrent">
                      {data.current.annotated_count +
                        data.current.skipped_count}
                    </span>
                  ),
                },
                {
                  label: "Published audio",
                  value: duration(data.current.duration_seconds),
                },
                { label: "Revoked", value: data.history.revoked_count },
                {
                  label: "Median turnaround",
                  value:
                    data.efficiency.turnaround_median_seconds === null
                      ? "—"
                      : duration(data.efficiency.turnaround_median_seconds),
                },
              ]}
            />
            {data.assignment && (
              <Alert
                type="info"
                title="Current assignment"
                description={`${data.assignment.filename ?? data.assignment.task_id ?? "Audio task"} · assigned ${dateTime(data.assignment.assigned_at)}`}
              />
            )}
            <Card title="Claim scope">
              <ScopeEditor
                key={`${id}:${data.scene_scope.revision}`}
                data={data}
              />
            </Card>
            <AdminList
              title="Current annotations"
              extra={
                <Button
                  id="batchRevokeButton"
                  danger
                  size="small"
                  disabled={!selected.length}
                  onClick={() => revoke(selected)}
                >
                  Revoke selected ({selected.length})
                </Button>
              }
            >
              <QueryState query={annotations}>
                <AdminTable
                  rowKey="version_id"
                  dataSource={rows}
                  scroll={{ x: 760 }}
                  rowSelection={{
                    selectedRowKeys: selection,
                    onChange: setSelection,
                    getCheckboxProps: (row) => ({ disabled: !row.can_revoke }),
                  }}
                  columns={[
                    {
                      title: "Audio",
                      render: (_, row) => (
                        <span className="admin-filename" title={row.rel_path}>
                          {row.filename}
                        </span>
                      ),
                    },
                    {
                      title: "Status",
                      render: (_, row) => <Status value={row.status} />,
                    },
                    {
                      title: "Duration",
                      align: "right",
                      render: (_, row) => duration(row.duration),
                    },
                    {
                      title: "Submitted",
                      className: "admin-table-meta",
                      render: (_, row) => dateTime(row.submitted_at),
                    },
                    {
                      title: "Actions",
                      align: "left",
                      render: (_, row) => (
                        <div className="admin-row-actions">
                          <Button
                            size="small"
                            onClick={() => openTask(row.task_id)}
                          >
                            View
                          </Button>
                          {row.can_revoke && (
                            <Button
                              danger
                              size="small"
                              onClick={() => revoke([row])}
                            >
                              Revoke
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
                  {annotations.isPending
                    ? "Loading annotations…"
                    : `${rows.length} annotations${annotations.hasNextPage ? " loaded" : ""}`}
                </span>
                {annotations.hasNextPage && (
                  <Button
                    loading={annotations.isFetchingNextPage}
                    onClick={() => void annotations.fetchNextPage()}
                  >
                    Load more
                  </Button>
                )}
              </AdminListFooter>
            </AdminList>
            <Collapse
              items={[
                {
                  key: "activity",
                  label: "Daily output and label mix",
                  children: (
                    <QueryState query={series}>
                      {series.data && <ActivityChart data={series.data} />}
                      <p>
                        {data.current.annotated_count} annotated ·{" "}
                        {data.current.skipped_count} skipped ·{" "}
                        {data.history.active_days} active days
                      </p>
                    </QueryState>
                  ),
                },
              ]}
            />
          </div>
        )}
      </QueryState>
    </Drawer>
  );
}

function ScopeEditor({ data }: { data: AnnotatorDetail }): React.JSX.Element {
  const catalog = useAdminQuery("/api/admin/metadata/facets", facetsSchema);
  const [mode, setMode] = useState(data.scene_scope.mode);
  const [codes, setCodes] = useState(data.scene_scope.scene_codes);
  const [reason, setReason] = useState("");
  const write = useAdminWrite(
    `/api/admin/annotators/${encodeURIComponent(data.id)}/scene-scope`,
    "PUT",
  );
  const { message } = App.useApp();
  const ready = !!catalog.data && !catalog.isError && !catalog.isPending;
  return (
    <QueryState query={catalog}>
      <Form
        id="sceneScopeForm"
        data-annotator-id={data.id}
        data-revision={data.scene_scope.revision}
        data-scope-ready={ready ? "1" : "0"}
        layout="vertical"
        disabled={!ready || write.pending || write.frozen}
        onFinish={() => {
          void write
            .submit({
              expected_revision: data.scene_scope.revision,
              mode,
              scene_codes: mode === "restricted" ? codes : [],
              allow_unknown:
                mode === "restricted" && codes.includes("spoken_languages"),
              reason: reason.trim(),
            })
            .then((success) => {
              if (success)
                void message.success(
                  "Claim scope saved. Existing assignments are not released.",
                );
            });
        }}
      >
        <p className="admin-muted">
          Controls future claims. Existing assignments remain with the
          annotator.
        </p>
        <Form.Item label="Scope mode">
          <Select
            id="scopeMode"
            value={mode}
            onChange={setMode}
            options={[
              { value: "all", label: "All source scenes" },
              { value: "restricted", label: "Restricted scenes" },
              { value: "none", label: "Cannot claim" },
            ]}
          />
        </Form.Item>
        {mode === "restricted" && (
          <Form.Item label="Allowed scenes">
            <div id="scopeSceneGrid">
              <Checkbox.Group
                value={codes}
                onChange={setCodes}
                className="admin-scenes"
                options={
                  catalog.data?.scenes.map((scene) => ({
                    value: scene.code,
                    label: scene.label_en,
                  })) ?? []
                }
              />
            </div>
            <p className="admin-muted">
              Spoken languages also permits tasks without a known source scene.
            </p>
          </Form.Item>
        )}
        <Form.Item label="Reason" required>
          <Input
            id="scopeReason"
            value={reason}
            maxLength={4000}
            onChange={(event) => setReason(event.target.value)}
            placeholder="Explain why this claim scope is changing"
          />
        </Form.Item>
        <WriteError error={write.error} frozen={write.frozen} />
        <Button
          htmlType="submit"
          type="primary"
          disabled={
            !ready || !reason.trim() || (mode === "restricted" && !codes.length)
          }
          loading={write.pending}
        >
          {write.frozen ? "Retry same change" : "Save claim scope"}
        </Button>
      </Form>
    </QueryState>
  );
}
