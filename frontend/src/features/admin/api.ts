import {
  createContext,
  useContext,
  useEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { z } from "zod";
import { ApiError, requestJson, shouldRetryGet } from "../../api/client";
import {
  pageSchema,
  annotatorSchema,
  writeResultSchema,
  type AdminAction,
  type AdminIdentity,
} from "./schemas";

export type Filters = Record<string, string>;

export function apiPath(path: string, filters: Filters = {}): string {
  const search = new URLSearchParams(
    Object.entries(filters).filter(([, value]) => value !== ""),
  );
  return search.size ? `${path}?${search}` : path;
}

export function createAdminClient(
  identity: AdminIdentity,
  onExpired: () => void,
) {
  async function read<T>(
    path: string,
    schema: z.ZodType<T>,
    signal?: AbortSignal,
  ): Promise<T> {
    try {
      return schema.parse(await requestJson<unknown>(path, { signal }));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onExpired();
      throw error;
    }
  }
  async function send<T>(
    path: string,
    schema: z.ZodType<T>,
    body: unknown,
    method = "POST",
  ): Promise<T> {
    try {
      return schema.parse(
        await requestJson<unknown>(path, {
          method,
          body,
          headers: { "X-CSRF-Token": identity.csrf_token },
        }),
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) onExpired();
      throw error;
    }
  }
  return { read, send };
}

export const AdminContext = createContext<{
  client: ReturnType<typeof createAdminClient>;
  identity: AdminIdentity;
  openTask: (taskId: string) => void;
  action: (action: AdminAction) => void;
  expire: () => void;
  refreshGuard: RefObject<(() => boolean) | null>;
} | null>(null);

export function useAdmin() {
  const value = useContext(AdminContext);
  if (!value) throw new Error("Admin provider is missing");
  return value;
}

export function useAdminQuery<T>(
  path: string,
  schema: z.ZodType<T>,
  filters: Filters = {},
  enabled = true,
) {
  const { client, identity } = useAdmin();
  const url = apiPath(path, filters);
  return useQuery({
    queryKey: ["admin", identity.key_id, url],
    queryFn: ({ signal }) => client.read(url, schema, signal),
    retry: shouldRetryGet,
    enabled,
  });
}

export function useAdminPages<T extends z.ZodType>(
  path: string,
  schema: T,
  filters: Filters = {},
) {
  const { client, identity } = useAdmin();
  const url = apiPath(path, filters);
  return useInfiniteQuery({
    queryKey: ["admin", identity.key_id, "pages", url],
    queryFn: ({ signal, pageParam }) =>
      client.read(
        apiPath(path, { ...filters, limit: "50", cursor: pageParam ?? "" }),
        pageSchema(schema),
        signal,
      ),
    initialPageParam: "",
    getNextPageParam: (page) => page.next_cursor,
    retry: shouldRetryGet,
  });
}

export function messageFor(error: unknown): string {
  if (error instanceof z.ZodError)
    return "The server returned unexpected data. Refresh or try again.";
  return error instanceof Error
    ? error.message
    : "The request failed. Try again.";
}

export function useAnnotatorDirectory() {
  const query = useAdminPages("/api/admin/annotators", annotatorSchema);
  const { hasNextPage, isFetching, isError, fetchNextPage } = query;
  useEffect(() => {
    if (hasNextPage && !isFetching && !isError) void fetchNextPage();
  }, [hasNextPage, isFetching, isError, fetchNextPage]);
  return query;
}

export function operationId(): string {
  return crypto.randomUUID();
}

// Keep an ambiguous write's payload and operation ID together so retries are idempotent.
export function useAdminWrite(path: string, method = "POST") {
  const { client } = useAdmin();
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [frozen, setFrozen] = useState<object | null>(null);
  const [error, setError] = useState<unknown>(null);
  const sending = useRef(false);
  async function submit(
    body: object,
    onFailure?: (cause: unknown) => void,
  ): Promise<boolean> {
    if (sending.current) return false;
    sending.current = true;
    setPending(true);
    setError(null);
    const payload = frozen ?? { ...body, operation_id: operationId() };
    try {
      await client.send(path, writeResultSchema, payload, method);
      setFrozen(null);
      await queryClient.invalidateQueries({ queryKey: ["admin"] });
      return true;
    } catch (cause) {
      setError(cause);
      const rejected =
        cause instanceof ApiError && cause.status >= 400 && cause.status < 500;
      setFrozen(rejected ? null : payload);
      onFailure?.(cause);
      return false;
    } finally {
      sending.current = false;
      setPending(false);
    }
  }
  return { submit, pending, frozen: frozen !== null, error };
}

export function downloadCsv(
  filename: string,
  headers: string[],
  rows: unknown[][],
): void {
  const cell = (value: unknown) => {
    let result = String(value ?? "");
    if (/^[=+@\-\t\r]/.test(result)) result = `'${result}`;
    return `"${result.replaceAll('"', '""')}"`;
  };
  const blob = new Blob(
    [
      "\ufeff",
      [headers, ...rows].map((row) => row.map(cell).join(",")).join("\n"),
    ],
    { type: "text/csv;charset=utf-8" },
  );
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600)
    return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`;
}

export function dateTime(value: string | null | undefined): string {
  return value
    ? new Date(value).toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : "—";
}

export function label(value: string): string {
  return value
    .replaceAll("_", " ")
    .replace(/^./, (letter) => letter.toUpperCase());
}
