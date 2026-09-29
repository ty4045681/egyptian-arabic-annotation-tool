/**
 * Admin Corpus query codec + date-boundary pure logic.
 *
 * Mirrors the legacy `admin.js` semantics without depending on the DOM:
 * - `commonQuery`: the UI end date (`to`) is *inclusive*; the API `to` is an
 *   exclusive upper bound built by shifting the UI date +1 day
 *   (`shiftDate(state.dateTo, 1)`), always with an explicit timezone. The
 *   backend interprets naive `from`/`to` as local midnight in that timezone
 *   (annotation_repository.py `_parse_admin_datetime`), so the frontend must
 *   send `timezone=Asia/Shanghai` and must NOT convert the UI date through
 *   the browser UTC clock.
 * - `restoreUrlState` / `syncUrl`: the URL keeps `view=corpus`, `range`,
 *   `from`/`to` (only when `range=custom`), `q`, and `status`.
 * - corpus default range is the legacy `30d` (`state.range` initial value);
 *   the cross-check all-time rule must not be copied over.
 *
 * BOUNDARY vs the old entry: the new page additionally persists the advanced
 * metadata filters (`source_scene`, `source_confidence`, `batch_code`,
 * `review_status`, `prediction_scene`, `human_scene`) in the URL using the
 * backend query names. The old `/admin` entry does not restore those from
 * the URL (it only restores `q`/`status`), so deep links with advanced
 * filters show the unfiltered-equivalent view there. Documented here and in
 * the page component; no backend change.
 */

export const CORPUS_VIEW = "corpus";
export const CORPUS_TIMEZONE = "Asia/Shanghai";
export const CORPUS_DEFAULT_RANGE = "30d";
export const CORPUS_PAGE_LIMIT = 50;
export const CORPUS_QUERY_ROLE = "admin";

export type CorpusRange = "7d" | "30d" | "90d" | "all" | "custom";

export type CorpusStatusFilter =
  | ""
  | "pending"
  | "assigned"
  | "annotated"
  | "skipped";

export const CORPUS_RANGES: readonly CorpusRange[] = [
  "7d",
  "30d",
  "90d",
  "all",
  "custom",
];

export const CORPUS_STATUS_VALUES: readonly string[] = [
  "",
  "pending",
  "assigned",
  "annotated",
  "skipped",
];

export const CORPUS_CONFIDENCE_VALUES: readonly string[] = [
  "",
  "high",
  "medium",
  "low",
  "unknown",
];

/** Review statuses accepted by the backend metadata filter, plus "" = all. */
export const CORPUS_REVIEW_STATUS_VALUES: readonly string[] = [
  "",
  "pending",
  "confirmed",
  "mixed",
  "out_of_scope",
  "uncertain",
  "unreviewed_unpublished",
  "unreviewed_published",
];

/** All corpus filters, already in UI representation (inclusive `to`). */
export interface CorpusFilters {
  range: CorpusRange;
  from: string;
  to: string;
  q: string;
  status: CorpusStatusFilter;
  assignee_id: string;
  source_scene: string;
  source_confidence: string;
  batch_code: string;
  review_status: string;
  prediction_scene: string;
  human_scene: string;
}

export type CorpusFilterInput = Partial<
  Record<keyof CorpusFilters, string | null | undefined>
>;

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

function cleanString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function splitCalendarDate(
  text: string,
): { year: number; month: number; day: number } | null {
  const parts = text.split("-");
  if (parts.length !== 3) return null;
  const year = Number(parts[0]);
  const month = Number(parts[1]);
  const day = Number(parts[2]);
  if (
    !Number.isInteger(year) ||
    !Number.isInteger(month) ||
    !Number.isInteger(day)
  ) {
    return null;
  }
  return { year, month, day };
}

function cleanDate(value: unknown): string {
  const text = cleanString(value);
  if (!DATE_PATTERN.test(text)) return "";
  const parsed = splitCalendarDate(text);
  if (!parsed) return "";
  const { year, month, day } = parsed;
  if (month < 1 || month > 12 || day < 1 || day > 31) return "";
  const probe = new Date(Date.UTC(year, month - 1, day));
  if (
    probe.getUTCFullYear() !== year ||
    probe.getUTCMonth() !== month - 1 ||
    probe.getUTCDate() !== day
  ) {
    return "";
  }
  return text;
}

function cleanEnum(
  value: unknown,
  allowed: readonly string[],
  fallback: string,
): string {
  const text = cleanString(value);
  return allowed.includes(text) ? text : fallback;
}

/**
 * Shift a `YYYY-MM-DD` calendar date by whole days using the UTC calendar
 * (identical to legacy `admin.js` `shiftDate`). Returns "" for invalid input
 * so callers never emit a corrupt `to` boundary.
 */
export function shiftDateString(dateString: string, days: number): string {
  if (!DATE_PATTERN.test(dateString)) return "";
  const parsed = splitCalendarDate(dateString);
  if (!parsed) return "";
  const { year, month, day } = parsed;
  const date = new Date(Date.UTC(year, month - 1, day));
  if (
    Number.isNaN(date.getTime()) ||
    date.getUTCFullYear() !== year ||
    date.getUTCMonth() !== month - 1 ||
    date.getUTCDate() !== day
  ) {
    return "";
  }
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

/**
 * Normalize raw filter input (URL params, form state) into canonical UI
 * filters. Unknown enum values fall back instead of leaking into the API.
 */
export function normalizeCorpusFilters(
  input: CorpusFilterInput | null | undefined,
): CorpusFilters {
  const raw = input ?? {};
  return {
    range: cleanEnum(
      raw.range,
      CORPUS_RANGES,
      CORPUS_DEFAULT_RANGE,
    ) as CorpusRange,
    from: cleanDate(raw.from),
    to: cleanDate(raw.to),
    q: cleanString(raw.q),
    status: cleanEnum(
      raw.status,
      CORPUS_STATUS_VALUES,
      "",
    ) as CorpusStatusFilter,
    assignee_id: cleanString(raw.assignee_id),
    source_scene: [
      ...new Set(
        cleanString(raw.source_scene)
          .split(",")
          .map((value) => value.trim())
          .filter(Boolean),
      ),
    ]
      .sort()
      .join(","),
    source_confidence: cleanEnum(
      raw.source_confidence,
      CORPUS_CONFIDENCE_VALUES,
      "",
    ),
    batch_code: cleanString(raw.batch_code),
    review_status: cleanEnum(
      raw.review_status,
      CORPUS_REVIEW_STATUS_VALUES,
      "",
    ),
    prediction_scene: cleanString(raw.prediction_scene),
    human_scene: cleanString(raw.human_scene),
  };
}

/** Default UI filters: legacy corpus range, everything else unfiltered. */
export function defaultCorpusFilters(): CorpusFilters {
  return normalizeCorpusFilters({ range: CORPUS_DEFAULT_RANGE });
}

export interface PresetDates {
  from: string;
  to: string;
}

/**
 * Resolve a date preset against a `today` string (`YYYY-MM-DD`, local
 * calendar day). `all` clears the bounds; `custom` keeps the current bounds
 * (the caller validates them on apply). Mirrors `applyPreset`, including the
 * 7/30/90-day windows ending today.
 */
export function applyDatePreset(
  range: CorpusRange,
  today: string,
  current: PresetDates,
): PresetDates {
  if (range === "all") return { from: "", to: "" };
  if (range === "custom") return { from: current.from, to: current.to };
  const days = range === "7d" ? 7 : range === "90d" ? 90 : 30;
  const cleanToday = cleanDate(today);
  if (!cleanToday) return { from: "", to: "" };
  return {
    from: shiftDateString(cleanToday, -(days - 1)),
    to: cleanToday,
  };
}

/**
 * Build the `GET /api/admin/tasks` query from UI filters.
 *
 * - UI `to` is inclusive: the API `to` is `shiftDateString(to, +1)` to form
 *   the exclusive upper bound (legacy `commonQuery`). Never pass the UI `to`
 *   through unchanged and never convert it via the browser UTC clock.
 * - `timezone` is always `Asia/Shanghai`; `bucket` follows the legacy rule
 *   (`week` for `90d`, else `day`) for overview parity.
 * - `range=all` sends no `from`/`to`.
 * - `limit` defaults to 50. `cursor` is only ever sent when appending within
 *   the same normalized filter set (the server binds it to `filter_digest`
 *   and rejects cross-filter cursors with 400).
 */
export function buildCorpusApiParams(
  filters: CorpusFilters,
  options?: { cursor?: string | null | undefined; limit?: number | undefined },
): Record<string, string> {
  const normalized = normalizeCorpusFilters(filters);
  const params: Record<string, string> = {};
  if (normalized.range !== "all") {
    if (normalized.from) params.from = normalized.from;
    if (normalized.to) {
      const exclusiveTo = shiftDateString(normalized.to, 1);
      if (exclusiveTo) params.to = exclusiveTo;
    }
  }
  params.timezone = CORPUS_TIMEZONE;
  params.bucket = normalized.range === "90d" ? "week" : "day";
  params.limit = String(options?.limit ?? CORPUS_PAGE_LIMIT);
  if (normalized.assignee_id) params.assignee_id = normalized.assignee_id;
  if (normalized.q) params.q = normalized.q;
  if (normalized.status) params.status = normalized.status;
  if (normalized.source_scene) params.source_scene = normalized.source_scene;
  if (normalized.source_confidence) {
    params.source_confidence = normalized.source_confidence;
  }
  if (normalized.batch_code) params.batch_code = normalized.batch_code;
  if (normalized.review_status) params.review_status = normalized.review_status;
  if (normalized.prediction_scene) {
    params.prediction_scene = normalized.prediction_scene;
  }
  if (normalized.human_scene) params.human_scene = normalized.human_scene;
  const cursor = cleanString(options?.cursor);
  if (cursor) params.cursor = cursor;
  return params;
}

/**
 * Encode UI filters into the page URL. Mirrors `syncUrl` for the corpus
 * view: `view`/`range` always, `from`/`to` only for `custom`, plus `q` and
 * `status` when set. Advanced filters use the backend query names (new vs
 * the old entry; see module doc).
 */
export function encodeCorpusUrlSearch(filters: CorpusFilters): string {
  const normalized = normalizeCorpusFilters(filters);
  const params = new URLSearchParams();
  params.set("view", CORPUS_VIEW);
  params.set("range", normalized.range);
  if (normalized.range === "custom") {
    if (normalized.from) params.set("from", normalized.from);
    if (normalized.to) params.set("to", normalized.to);
  }
  if (normalized.assignee_id) params.set("assignee_id", normalized.assignee_id);
  if (normalized.q) params.set("q", normalized.q);
  if (normalized.status) params.set("status", normalized.status);
  if (normalized.source_scene) {
    params.set("source_scene", normalized.source_scene);
  }
  if (normalized.source_confidence) {
    params.set("source_confidence", normalized.source_confidence);
  }
  if (normalized.batch_code) params.set("batch_code", normalized.batch_code);
  if (normalized.review_status) {
    params.set("review_status", normalized.review_status);
  }
  if (normalized.prediction_scene) {
    params.set("prediction_scene", normalized.prediction_scene);
  }
  if (normalized.human_scene) params.set("human_scene", normalized.human_scene);
  return params.toString();
}

/**
 * Decode the page URL into UI filters. Mirrors `restoreUrlState`: `from`/`to`
 * are only honored for `range=custom`; other ranges resolve via
 * `applyDatePreset`. Unknown `view` values fall back to corpus.
 */
export function decodeCorpusUrlSearch(
  search: string,
  today: string,
): CorpusFilters {
  const params = new URLSearchParams(
    search.startsWith("?") ? search : `?${search}`,
  );
  const view = cleanString(params.get("view"));
  const range = cleanEnum(
    params.get("range"),
    CORPUS_RANGES,
    CORPUS_DEFAULT_RANGE,
  ) as CorpusRange;
  const base = normalizeCorpusFilters({
    range,
    from: range === "custom" ? params.get("from") : "",
    to: range === "custom" ? params.get("to") : "",
    q: view === CORPUS_VIEW || !view ? params.get("q") : "",
    status: view === CORPUS_VIEW || !view ? params.get("status") : "",
    assignee_id: params.get("assignee_id"),
    source_scene: params.get("source_scene"),
    source_confidence: params.get("source_confidence"),
    batch_code: params.get("batch_code"),
    review_status: params.get("review_status"),
    prediction_scene: params.get("prediction_scene"),
    human_scene: params.get("human_scene"),
  });
  if (base.range !== "custom" && base.range !== "all") {
    const preset = applyDatePreset(base.range, today, { from: "", to: "" });
    base.from = preset.from;
    base.to = preset.to;
  }
  return base;
}

/**
 * Stable request key for response attribution: a late response whose key no
 * longer matches the current key must be ignored (in addition to aborting
 * the request). The cursor is part of the key so an append response can never
 * satisfy a fresh-filter request.
 */
export function makeCorpusRequestKey(
  filters: CorpusFilters,
  cursor?: string | null,
): string {
  const normalized = normalizeCorpusFilters(filters);
  return JSON.stringify([normalized, cleanString(cursor)]);
}

/** True when an arrived response no longer belongs to the current request. */
export function isStaleCorpusResponse(
  responseKey: string,
  currentKey: string,
): boolean {
  return responseKey !== currentKey;
}

export interface CorpusQueryKeyContext {
  /** Admin role scope (fixed "admin"); part of the key per spec. */
  role?: string;
  /** Admin session identity (`key_id` from `/api/admin/session`). */
  sessionKey?: string | null;
  filters: CorpusFilters;
}

/**
 * TanStack Query key for the corpus list. Contains the role, the session
 * scope, and every normalized filter; the cursor is intentionally excluded
 * (pagination state lives in the infinite-query pages, and the server binds
 * each cursor to its `filter_digest` anyway). Changing identity must drop
 * this role's cached pages (see the page component).
 */
export function buildCorpusQueryKey(
  context: CorpusQueryKeyContext,
): readonly [string, string, string, string, CorpusFilters] {
  return [
    "admin",
    "corpus",
    context.role ?? CORPUS_QUERY_ROLE,
    cleanString(context.sessionKey),
    normalizeCorpusFilters(context.filters),
  ];
}

/** HTTP statuses that must never be retried (auth/client errors). */
export function isNonRetryableCorpusStatus(status: unknown): boolean {
  return status === 400 || status === 401 || status === 403;
}

export function corpusRequestStatusOf(error: unknown): number | undefined {
  if (typeof error !== "object" || error === null) return undefined;
  const record = error as Record<string, unknown>;
  for (const key of ["status", "statusCode", "code"]) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  const response = record.response;
  if (typeof response === "object" && response !== null) {
    const status = (response as Record<string, unknown>).status;
    if (typeof status === "number" && Number.isFinite(status)) return status;
  }
  return undefined;
}

/**
 * Read-only GET retry rule: 401/403/400 never retry; transient failures at
 * most twice after the first attempt. Window-focus refetch is disabled
 * separately in the query options so refocusing never reorders the list.
 */
export function shouldRetryCorpusRequest(
  failureCount: number,
  error: unknown,
): boolean {
  if (failureCount >= 2) return false;
  return !isNonRetryableCorpusStatus(corpusRequestStatusOf(error));
}
