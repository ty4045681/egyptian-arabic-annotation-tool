/**
 * Shared P1 TanStack Query keys (work packages A/B, src/api).
 *
 * Every key contains the role, the session scope, and all normalized
 * filters. Cursors are never part of the key: pagination lives in the
 * infinite-query pages and the server binds each cursor to its
 * `filter_digest` (cross-filter cursors are rejected with 400). Changing
 * identity must drop that role's cached pages.
 *
 * Shapes intentionally match
 * `features/admin/corpus/queryCodec.ts::buildCorpusQueryKey` so the corpus
 * page and this shared module address the same cache entries:
 * `["admin", "corpus", role, sessionKey, normalizedFilters]`.
 */

export const QUERY_ROLE_ADMIN = "admin";
export const QUERY_ROLE_ANNOTATOR = "annotator";

export interface CorpusKeyFilters {
  range?: string | undefined;
  from?: string | undefined;
  to?: string | undefined;
  q?: string | undefined;
  status?: string | undefined;
  source_scene?: string | undefined;
  source_confidence?: string | undefined;
  batch_code?: string | undefined;
  review_status?: string | undefined;
  prediction_scene?: string | undefined;
  human_scene?: string | undefined;
  [field: string]: unknown;
}

function cleanKeyPart(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** `["admin", "session", sessionKey]` — independent admin identity check. */
export function adminSessionKey(
  sessionKey?: string | null | undefined,
): readonly [string, string, string] {
  return ["admin", "session", cleanKeyPart(sessionKey)];
}

/** `["admin", "facets", sessionKey]` — filter vocabulary/options. */
export function adminFacetsKey(
  sessionKey?: string | null | undefined,
): readonly [string, string, string] {
  return ["admin", "facets", cleanKeyPart(sessionKey)];
}

/**
 * `["admin", "corpus", role, sessionKey, filters]` — server-cursor list.
 * `filters` must already be normalized (see `normalizeCorpusFilters`);
 * this helper does not normalize, it only freezes the reference shape.
 */
export function adminCorpusKey(
  filters: CorpusKeyFilters,
  sessionKey?: string | null | undefined,
  role: string = QUERY_ROLE_ADMIN,
): readonly [string, string, string, string, CorpusKeyFilters] {
  return ["admin", "corpus", role, cleanKeyPart(sessionKey), { ...filters }];
}

/** `["annotator", "assignment", username]` — current assignment/session. */
export function assignmentKey(
  username?: string | null | undefined,
): readonly [string, string, string] {
  return [QUERY_ROLE_ANNOTATOR, "assignment", cleanKeyPart(username)];
}

/** `["annotator", "scenes", username]` — taxonomy/flags for the assignee. */
export function scenesKey(
  username?: string | null | undefined,
): readonly [string, string, string] {
  return [QUERY_ROLE_ANNOTATOR, "scenes", cleanKeyPart(username)];
}

/** `["annotator", "current-user"]` — annotator session probe. */
export function currentUserKey(): readonly [string, string] {
  return [QUERY_ROLE_ANNOTATOR, "current-user"];
}
