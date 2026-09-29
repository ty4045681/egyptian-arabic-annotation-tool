import { metadataChips } from "../metadataFilterModel";
import { dateChip, filterToday } from "../filterModel";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { Alert, Button, Select } from "antd";
import { downloadCsv, duration, type Filters } from "../api";
import {
  AdminFilterBar,
  DateRangeFilter,
  FilterChoices,
} from "../AdminFilters";
import { MetadataFilters } from "../MetadataFilters";
import {
  AsyncEmpty,
  AsyncError,
  AsyncLoading,
} from "../../../components/AsyncState";
import { PageHeader } from "../../../components/PageHeader";
import {
  CORPUS_ENDPOINTS,
  fetchAdminSession,
  fetchCorpusAssignees,
  fetchCorpusFacets,
  fetchCorpusTasks,
  normalizeCorpusError,
  type CorpusApiClient,
} from "./api";
import { CorpusTable } from "./components/CorpusTable";
import { AdminList } from "../AdminList";
import {
  applyDatePreset,
  buildCorpusQueryKey,
  CORPUS_DEFAULT_RANGE,
  decodeCorpusUrlSearch,
  defaultCorpusFilters,
  encodeCorpusUrlSearch,
  normalizeCorpusFilters,
  shouldRetryCorpusRequest,
  type CorpusFilters,
} from "./queryCodec";
import type { CorpusTaskItem } from "./types";
import styles from "./CorpusPage.module.css";

export interface CorpusPageProps {
  apiClient: CorpusApiClient;
  /** Initial URL search; defaults to `window.location.search`. */
  initialSearch?: string;
  /** Local `YYYY-MM-DD` used to resolve presets; defaults to today. */
  today?: string;
  /** Login fallback for the standalone read-only preview. */
  loginHref?: string;
  onSessionInvalid?: () => void;
  onViewTask?: ((taskId: string) => void) | undefined;
  onRevoke?: ((task: CorpusTaskItem) => void) | undefined;
  onSearchChange?: ((search: string) => void) | undefined;
}

function currentSearch(): string {
  if (typeof window === "undefined") return "";
  return window.location.search || "";
}

function errorMessage(error: unknown, fallback: string): string {
  const normalized = normalizeCorpusError(error);
  return normalized.message || fallback;
}

export function CorpusPage({
  apiClient,
  initialSearch,
  today,
  loginHref = CORPUS_ENDPOINTS.loginPage,
  onSessionInvalid,
  onViewTask,
  onRevoke,
  onSearchChange,
}: CorpusPageProps): React.JSX.Element {
  const queryClient = useQueryClient();
  const resolvedToday = today ?? filterToday();
  const [applied, setApplied] = useState<CorpusFilters>(() =>
    decodeCorpusUrlSearch(initialSearch ?? currentSearch(), resolvedToday),
  );
  const [sessionInvalid, setSessionInvalid] = useState(false);
  const sessionKeyRef = useRef<string>("");

  // Keep the URL shareable/reloadable with corpus semantics.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const nextSearch = encodeCorpusUrlSearch(applied);
    if (onSearchChange) onSearchChange(nextSearch);
    else
      window.history.replaceState(
        window.history.state,
        "",
        `${window.location.pathname}?${nextSearch}`,
      );
  }, [applied, onSearchChange]);

  useEffect(() => {
    const restore = () => {
      const filters = decodeCorpusUrlSearch(currentSearch(), resolvedToday);
      setApplied(filters);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [resolvedToday]);

  const sessionQuery = useQuery({
    queryKey: ["admin", "session"],
    queryFn: ({ signal }) => fetchAdminSession(apiClient, signal),
    retry: (failureCount, error) =>
      shouldRetryCorpusRequest(failureCount, error),
    refetchOnWindowFocus: false,
  });

  const sessionKey = sessionQuery.data?.key_id
    ? String(sessionQuery.data.key_id)
    : "";

  // A different admin identity must not reuse this role's cached pages.
  useEffect(() => {
    if (!sessionKey) return;
    if (sessionKeyRef.current && sessionKeyRef.current !== sessionKey) {
      void queryClient.removeQueries({ queryKey: ["admin", "corpus"] });
    }
    sessionKeyRef.current = sessionKey;
  }, [queryClient, sessionKey]);

  const facetsQuery = useQuery({
    queryKey: ["admin", "facets", sessionKey],
    queryFn: ({ signal }) => fetchCorpusFacets(apiClient, signal),
    retry: (failureCount, error) =>
      shouldRetryCorpusRequest(failureCount, error),
    refetchOnWindowFocus: false,
    enabled: !sessionInvalid,
  });

  const tasksQuery = useInfiniteQuery({
    queryKey: buildCorpusQueryKey({ sessionKey, filters: applied }),
    queryFn: ({ pageParam, signal }) =>
      fetchCorpusTasks(apiClient, applied, {
        cursor: (pageParam as string | null) ?? null,
        signal,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    retry: (failureCount, error) =>
      shouldRetryCorpusRequest(failureCount, error),
    refetchOnWindowFocus: false,
    enabled: !sessionInvalid,
  });

  const invalidateSession = (): void => {
    setSessionInvalid(true);
    // Drop sensitive list state and the cached identity/pages.
    void queryClient.removeQueries({ queryKey: ["admin", "corpus"] });
    void queryClient.removeQueries({ queryKey: ["admin", "session"] });
    setApplied((previous) => ({ ...previous, q: "" }));
    onSessionInvalid?.();
  };

  // Session expiry clears sensitive rows and returns to login.
  useEffect(() => {
    if (sessionInvalid) return;
    const sessionStatus = sessionQuery.error
      ? normalizeCorpusError(sessionQuery.error).status
      : undefined;
    if (sessionStatus === 401 || sessionStatus === 403) {
      invalidateSession();
      return;
    }
    const tasksStatus = tasksQuery.error
      ? normalizeCorpusError(tasksQuery.error).status
      : undefined;
    if (
      tasksStatus === 401 ||
      (tasksStatus === 403 && !tasksQuery.isFetchNextPageError)
    ) {
      invalidateSession();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionQuery.error, tasksQuery.error, sessionInvalid]);

  const sceneLabels = useMemo<Record<string, string>>(() => {
    const entries: Record<string, string> = {};
    for (const scene of facetsQuery.data?.scenes ?? []) {
      entries[scene.code] = scene.label_en || scene.code;
    }
    return entries;
  }, [facetsQuery.data]);

  const items = useMemo<CorpusTaskItem[]>(
    () => (tasksQuery.data?.pages ?? []).flatMap((page) => page.items),
    [tasksQuery.data],
  );

  const firstPage = tasksQuery.data?.pages[0];
  const matchedCount = firstPage?.matched_count ?? 0;
  const matchedDuration = firstPage?.matched_duration_seconds ?? 0;

  const facetsUnavailable =
    facetsQuery.isPending || facetsQuery.isError || sessionInvalid;
  const facetsNotice = facetsQuery.isError
    ? `Filter options failed to load: ${errorMessage(facetsQuery.error, "Could not load filter options.")}`
    : null;

  const assignees = useQuery({
    queryKey: ["admin", "corpus-assignees", sessionKey],
    queryFn: ({ signal }) => fetchCorpusAssignees(apiClient, signal),
    enabled: !sessionInvalid,
    retry: shouldRetryCorpusRequest,
    refetchOnWindowFocus: false,
  });
  const options = {
    scenes: (facetsQuery.data?.scenes ?? []).map((scene) => ({
      value: scene.code,
      label: scene.label_en || scene.code,
    })),
    batches: (facetsQuery.data?.batches ?? []).map((batch) => ({
      value: batch.batch_code,
      label: batch.batch_code,
    })),
  };
  const commitFilters = (patch: Filters): void =>
    setApplied((old) => normalizeCorpusFilters({ ...old, ...patch }));
  const handleReset = (): void =>
    setApplied({
      ...defaultCorpusFilters(),
      ...applyDatePreset(CORPUS_DEFAULT_RANGE, resolvedToday, {
        from: "",
        to: "",
      }),
    });
  const counts = firstPage?.filter_counts ?? undefined;
  const statusOptions = [
    { value: "", label: "All" },
    { value: "pending", label: "Pending", color: "#df9914" },
    { value: "annotated", label: "Annotated", color: "#22a06b" },
    { value: "skipped", label: "Skipped", color: "#94a3b8" },
    { value: "assigned", label: "Assigned", color: "#6787dc" },
  ];
  const assigneeName =
    applied.assignee_id === "unassigned"
      ? "Unassigned"
      : (assignees.data?.find((person) => person.id === applied.assignee_id)
          ?.username ?? applied.assignee_id);
  const chips = [
    ...dateChip(applied, () =>
      commitFilters({ range: "all", from: "", to: "" }),
    ),
    ...(applied.q
      ? [
          {
            key: "q",
            label: `Search: ${applied.q}`,
            onRemove: () => commitFilters({ q: "" }),
          },
        ]
      : []),
    ...(applied.status
      ? [
          {
            key: "status",
            label: `Status: ${statusOptions.find((item) => item.value === applied.status)?.label ?? applied.status}`,
            onRemove: () => commitFilters({ status: "" }),
          },
        ]
      : []),
    ...(applied.assignee_id
      ? [
          {
            key: "assignee",
            label: `Assignee: ${assigneeName}`,
            onRemove: () => commitFilters({ assignee_id: "" }),
          },
        ]
      : []),
    ...metadataChips({ ...applied }, options, commitFilters),
  ];

  const firstScreenFailed =
    tasksQuery.status === "error" && !tasksQuery.isFetchNextPageError;
  const appendFailed = tasksQuery.isFetchNextPageError;

  return (
    <div className={styles.page} data-testid="corpus-page">
      <PageHeader
        title="Tasks & corpus"
        description="Inspect current task state across the complete audio collection."
      />

      {sessionInvalid ? (
        <div role="alert" data-testid="corpus-login-required">
          <p>
            Your admin session expired. The list and search query were cleared.
          </p>
          <p>
            <a href={loginHref}>Sign in via the admin login</a>, then return to
            this preview.
          </p>
        </div>
      ) : (
        <>
          <AdminFilterBar
            label="Filter tasks"
            search={{
              value: applied.q,
              onChange: (q) => commitFilters({ q }),
              label: "Search audio tasks",
              placeholder: "Search filename or path…",
              testId: "corpus-q",
            }}
            primary={
              <>
                <DateRangeFilter
                  value={applied}
                  onChange={(dates) => commitFilters(dates)}
                  today={resolvedToday}
                  prefix="corpus"
                />
                <Select
                  aria-label="Assignee"
                  placeholder="All assignees"
                  allowClear
                  showSearch={{ optionFilterProp: "label" }}
                  value={applied.assignee_id || undefined}
                  disabled={!assignees.data || assignees.isError}
                  loading={assignees.isFetching}
                  onChange={(value: string | undefined) =>
                    commitFilters({ assignee_id: value ?? "" })
                  }
                  options={[
                    { value: "unassigned", label: "Unassigned" },
                    ...(assignees.data ?? []).map((person) => ({
                      value: person.id,
                      label: person.username,
                    })),
                  ]}
                />
              </>
            }
            onReset={handleReset}
            chips={chips}
            summary={
              tasksQuery.isFetching && !tasksQuery.isFetchingNextPage ? (
                "Updating results…"
              ) : tasksQuery.isError ? (
                "Results unavailable"
              ) : (
                <span data-testid="corpus-matched">
                  <strong>{matchedCount.toLocaleString()}</strong> tasks ·{" "}
                  {duration(matchedDuration)} audio
                </span>
              )
            }
            exportDisabled={
              !items.length || tasksQuery.isFetching || tasksQuery.isError
            }
            onExport={() =>
              downloadCsv(
                "corpus.csv",
                [
                  "filename",
                  "status",
                  "duration",
                  "submitter",
                  "source_scenes",
                  "submitted_at",
                ],
                items.map((item) => [
                  item.filename,
                  item.status,
                  item.duration,
                  item.current_submitter?.username,
                  item.source_scenes?.join("; "),
                  item.submitted_at,
                ]),
              )
            }
          >
            <MetadataFilters
              leading={
                <div className="af-status-line">
                  <FilterChoices
                    label="Status"
                    value={applied.status}
                    options={statusOptions.map((option) => ({
                      ...option,
                      count: counts?.status[option.value || "all"],
                    }))}
                    onChange={(status) => commitFilters({ status })}
                    hint="Pending includes assigned tasks"
                  />
                  {counts && (
                    <div
                      className="af-distribution"
                      title="Pending includes assigned tasks"
                      aria-hidden="true"
                    >
                      {statusOptions.slice(1, 4).map((option) => (
                        <span
                          key={option.value}
                          style={{
                            background: option.color,
                            flexGrow: counts.status[option.value] ?? 0,
                          }}
                        />
                      ))}
                    </div>
                  )}
                </div>
              }
              values={{ ...applied }}
              options={options}
              apply={commitFilters}
              disabled={facetsUnavailable}
              counts={counts}
            />
            {facetsNotice && (
              <Alert
                type="error"
                title={facetsNotice}
                action={
                  <Button onClick={() => void facetsQuery.refetch()}>
                    Retry catalog
                  </Button>
                }
              />
            )}
            {assignees.isError && (
              <Alert
                type="error"
                title="Assignees unavailable"
                action={
                  <Button onClick={() => void assignees.refetch()}>
                    Retry assignees
                  </Button>
                }
              />
            )}
          </AdminFilterBar>

          <AdminList aria-label="Results">
            {tasksQuery.isPending ? (
              <AsyncLoading label="Loading tasks…" testId="corpus-state" />
            ) : firstScreenFailed ? (
              <AsyncError
                testId="corpus-error"
                retryTestId="corpus-retry"
                retryLabel="Retry"
                message={errorMessage(
                  tasksQuery.error,
                  "Could not load tasks.",
                )}
                onRetry={() => tasksQuery.refetch()}
              />
            ) : items.length === 0 ? (
              <AsyncEmpty
                testId="corpus-state"
                message="No tasks match these filters."
              />
            ) : (
              <>
                <CorpusTable
                  items={items}
                  sceneLabels={sceneLabels}
                  hasMore={tasksQuery.hasNextPage}
                  loadingMore={tasksQuery.isFetchingNextPage}
                  onLoadMore={() => tasksQuery.fetchNextPage()}
                  appendError={
                    appendFailed
                      ? errorMessage(
                          tasksQuery.error,
                          "Could not load more tasks.",
                        )
                      : null
                  }
                  onRetryAppend={() => tasksQuery.fetchNextPage()}
                  onViewTask={onViewTask}
                  onRevoke={onRevoke}
                />
              </>
            )}
          </AdminList>
        </>
      )}
    </div>
  );
}
