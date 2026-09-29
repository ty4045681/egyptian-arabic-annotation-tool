import {
  facetsSchema,
  pageSchema,
  personSchema,
  sessionSchema,
} from "../schemas";
import { z } from "zod";
import { buildCorpusApiParams, type CorpusFilters } from "./queryCodec";
import {
  corpusListSchema,
  type AdminSession,
  type CorpusFacets,
  type CorpusListResponse,
} from "./types";

export const CORPUS_ENDPOINTS = {
  session: "/api/admin/session",
  /** Legacy login entry (old management login; the P1 page adds no admin auth UI). */
  loginPage: "/admin/login",
  /** Legacy console entry (full navigation back to the old pages). */
  legacyConsole: "/admin?view=corpus",
  facets: "/api/admin/metadata/facets",
  tasks: "/api/admin/tasks",
} as const;

export interface CorpusApiClient {
  get(
    path: string,
    options?: { signal?: AbortSignal | undefined },
  ): Promise<unknown>;
}

/** Normalized request failure carrying the HTTP status when known. */
export class CorpusRequestError extends Error {
  readonly status?: number | undefined;
  readonly payload?: unknown;

  constructor(message: string, status?: number, payload?: unknown) {
    super(message);
    this.name = "CorpusRequestError";
    this.status = status;
    this.payload = payload;
  }
}

/** Wrap client rejections so retry/401 logic sees a stable shape. */
export function normalizeCorpusError(error: unknown): CorpusRequestError {
  if (error instanceof CorpusRequestError) return error;
  if (error instanceof z.ZodError)
    return new CorpusRequestError(
      "The server returned unexpected data. Refresh or try again.",
    );
  if (typeof error === "object" && error !== null) {
    const record = error as Record<string, unknown>;
    const statusRaw =
      record.status ??
      record.statusCode ??
      (typeof record.response === "object" && record.response !== null
        ? (record.response as Record<string, unknown>).status
        : undefined);
    const status =
      typeof statusRaw === "number" && Number.isFinite(statusRaw)
        ? statusRaw
        : undefined;
    const messageRaw =
      typeof record.message === "string" && record.message
        ? record.message
        : "Request failed.";
    if (record.name === "AbortError") {
      const aborted = new CorpusRequestError("Request was cancelled.");
      aborted.name = "AbortError";
      return aborted;
    }
    return new CorpusRequestError(messageRaw, status, record);
  }
  return new CorpusRequestError("Request failed.");
}

function withQuery(path: string, params: Record<string, string>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== "") search.set(key, value);
  }
  const query = search.toString();
  return query ? `${path}?${query}` : path;
}

/** Identity check; rejects with 401 (via the client) when signed out. */
export async function fetchAdminSession(
  client: CorpusApiClient,
  signal?: AbortSignal,
): Promise<AdminSession> {
  try {
    return sessionSchema.parse(
      await client.get(CORPUS_ENDPOINTS.session, {
        signal,
      }),
    );
  } catch (error) {
    throw normalizeCorpusError(error);
  }
}

/** Filter vocabulary/options (no counts/durations here by backend design). */
export async function fetchCorpusFacets(
  client: CorpusApiClient,
  signal?: AbortSignal,
): Promise<CorpusFacets> {
  try {
    return facetsSchema.parse(
      await client.get(CORPUS_ENDPOINTS.facets, {
        signal,
      }),
    );
  } catch (error) {
    throw normalizeCorpusError(error);
  }
}

export interface FetchCorpusTasksOptions {
  cursor?: string | null | undefined;
  limit?: number | undefined;
  signal?: AbortSignal | undefined;
}

/**
 * Fetch one page. `matched_count` / `matched_duration_seconds` come straight
 * from this response — never from an overview call and never from the number
 * of loaded rows.
 */
export async function fetchCorpusTasks(
  client: CorpusApiClient,
  filters: CorpusFilters,
  options?: FetchCorpusTasksOptions,
): Promise<CorpusListResponse> {
  const params = buildCorpusApiParams(filters, {
    cursor: options?.cursor,
    limit: options?.limit,
  });
  try {
    return corpusListSchema.parse(
      await client.get(withQuery(CORPUS_ENDPOINTS.tasks, params), {
        signal: options?.signal,
      }),
    );
  } catch (error) {
    throw normalizeCorpusError(error);
  }
}

export async function fetchCorpusAssignees(
  client: CorpusApiClient,
  signal: AbortSignal,
): Promise<z.infer<typeof personSchema>[]> {
  const people: z.infer<typeof personSchema>[] = [];
  let cursor: string | null = "";
  do {
    const raw = await client.get(
      withQuery("/api/admin/annotators", { limit: "100", cursor }),
      { signal },
    );
    const page = pageSchema(personSchema).parse(raw);
    people.push(...page.items);
    cursor = page.next_cursor;
  } while (cursor);
  return people;
}
