/**
 * Admin Corpus representative page — public entry.
 *
 * Consumed by `src/app` routing once the scaffold lands (preview route
 * `view=corpus`). Nothing outside `features/admin/corpus` is imported here
 * except React, TanStack Query (provided by the app providers), and the
 * local client stub documented in `api.ts`.
 */
export { CorpusPage, type CorpusPageProps } from "./CorpusPage";
export {
  buildCorpusApiParams,
  decodeCorpusUrlSearch,
  encodeCorpusUrlSearch,
  normalizeCorpusFilters,
  defaultCorpusFilters,
  applyDatePreset,
  shiftDateString,
  makeCorpusRequestKey,
  isStaleCorpusResponse,
  buildCorpusQueryKey,
  shouldRetryCorpusRequest,
  isNonRetryableCorpusStatus,
  CORPUS_VIEW,
  CORPUS_TIMEZONE,
  CORPUS_DEFAULT_RANGE,
  CORPUS_PAGE_LIMIT,
  type CorpusFilters,
  type CorpusRange,
  type CorpusStatusFilter,
} from "./queryCodec";
export {
  CORPUS_ENDPOINTS,
  fetchAdminSession,
  fetchCorpusFacets,
  fetchCorpusTasks,
  CorpusRequestError,
  normalizeCorpusError,
  type CorpusApiClient,
} from "./api";
export type {
  AdminSession,
  CorpusFacets,
  CorpusTaskItem,
  CorpusListResponse,
} from "./types";
