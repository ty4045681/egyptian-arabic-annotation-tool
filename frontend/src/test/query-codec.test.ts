/**
 * Corpus query-codec + date-boundary pure-logic tests (Vitest).
 *
 * Scope: query encoding/decoding and date boundaries only. No DOM, no
 * network, no component rendering. Fixture-heavy UI cases (long
 * English/Arabic filenames, long usernames, empty list, first-screen
 * failure, repeated-filter late responses) live in
 * `tests/browser/test_frontend_p1_corpus.py`.
 */
import { describe, expect, it, vi } from "vitest";
import { dashboardDates } from "../features/admin/dateFilters";
import { filterToday } from "../features/admin/filterModel";
import {
  applyDatePreset,
  buildCorpusApiParams,
  buildCorpusQueryKey,
  CORPUS_DEFAULT_RANGE,
  CORPUS_PAGE_LIMIT,
  CORPUS_TIMEZONE,
  decodeCorpusUrlSearch,
  defaultCorpusFilters,
  encodeCorpusUrlSearch,
  isNonRetryableCorpusStatus,
  isStaleCorpusResponse,
  makeCorpusRequestKey,
  normalizeCorpusFilters,
  shiftDateString,
  shouldRetryCorpusRequest,
} from "../features/admin/corpus/queryCodec";

describe("shiftDateString", () => {
  it("shifts the UI end date +1 day for the exclusive API bound", () => {
    expect(shiftDateString("2026-09-21", 1)).toBe("2026-09-22");
  });

  it("crosses month and year boundaries on the UTC calendar", () => {
    expect(shiftDateString("2026-01-31", 1)).toBe("2026-02-01");
    expect(shiftDateString("2025-12-31", 1)).toBe("2026-01-01");
    expect(shiftDateString("2024-02-28", 1)).toBe("2024-02-29");
  });

  it("builds preset windows ending today", () => {
    expect(shiftDateString("2026-09-21", -6)).toBe("2026-09-15");
    expect(shiftDateString("2026-09-21", -29)).toBe("2026-08-23");
  });

  it("rejects invalid input instead of emitting a corrupt boundary", () => {
    expect(shiftDateString("", 1)).toBe("");
    expect(shiftDateString("2026-13-01", 1)).toBe("");
    expect(shiftDateString("2026-02-30", 1)).toBe("");
    expect(shiftDateString("not-a-date", 1)).toBe("");
  });
});

describe("buildCorpusApiParams", () => {
  it("turns the inclusive UI end day into an exclusive API upper bound", () => {
    const params = buildCorpusApiParams(
      normalizeCorpusFilters({
        range: "custom",
        from: "2026-09-20",
        to: "2026-09-21",
      }),
    );
    expect(params.from).toBe("2026-09-20");
    expect(params.to).toBe("2026-09-22");
    expect(params.timezone).toBe(CORPUS_TIMEZONE);
  });

  it("always sends Asia/Shanghai and never the browser UTC conversion", () => {
    const params = buildCorpusApiParams(defaultCorpusFilters());
    expect(params.timezone).toBe("Asia/Shanghai");
    expect(params.limit).toBe(String(CORPUS_PAGE_LIMIT));
  });

  it("omits bounds for range=all and keeps the legacy bucket rule", () => {
    const all = buildCorpusApiParams(normalizeCorpusFilters({ range: "all" }));
    expect(all.from ?? "").toBe("");
    expect(all.to ?? "").toBe("");
    expect(all.bucket).toBe("day");
    const wide = buildCorpusApiParams(
      normalizeCorpusFilters({ range: "90d", from: "2026-06-01", to: "2026-09-21" }),
    );
    expect(wide.bucket).toBe("week");
  });

  it("passes backend filters through and only sends cursor when appending", () => {
    const base = normalizeCorpusFilters({
      range: "custom",
      from: "2026-09-01",
      to: "2026-09-21",
      q: "  airport  ",
      status: "annotated",
      source_scene: "airport",
      source_confidence: "high",
      batch_code: "batch-1",
      review_status: "confirmed",
      prediction_scene: "airport",
      human_scene: "shopping",
    });
    const params = buildCorpusApiParams(base);
    expect(params.q).toBe("airport");
    expect(params.status).toBe("annotated");
    expect(params.source_scene).toBe("airport");
    expect(params.source_confidence).toBe("high");
    expect(params.batch_code).toBe("batch-1");
    expect(params.review_status).toBe("confirmed");
    expect(params.prediction_scene).toBe("airport");
    expect(params.human_scene).toBe("shopping");
    expect(params.cursor ?? "").toBe("");
    const appended = buildCorpusApiParams(base, { cursor: "cursor-1" });
    expect(appended.cursor).toBe("cursor-1");
  });

  it("drops unknown enum values instead of leaking them to the API", () => {
    const params = buildCorpusApiParams(
      normalizeCorpusFilters({ status: "bogus", source_confidence: "bogus" }),
    );
    expect(params.status ?? "").toBe("");
    expect(params.source_confidence ?? "").toBe("");
  });
});

describe("date presets", () => {
  it("resolves 7d/30d windows ending today", () => {
    expect(
      applyDatePreset("7d", "2026-09-21", { from: "", to: "" }),
    ).toEqual({ from: "2026-09-15", to: "2026-09-21" });
    expect(
      applyDatePreset("30d", "2026-09-21", { from: "", to: "" }),
    ).toEqual({ from: "2026-08-23", to: "2026-09-21" });
  });

  it("clears bounds for all and keeps custom bounds untouched", () => {
    expect(
      applyDatePreset("all", "2026-09-21", { from: "x", to: "y" }),
    ).toEqual({ from: "", to: "" });
    expect(
      applyDatePreset(
        "custom",
        "2026-09-21",
        { from: "2026-09-01", to: "2026-09-10" },
      ),
    ).toEqual({ from: "2026-09-01", to: "2026-09-10" });
  });

  it("defaults to the legacy corpus range, not the cross-check rule", () => {
    expect(CORPUS_DEFAULT_RANGE).toBe("30d");
    expect(defaultCorpusFilters().range).toBe("30d");
  });
});

describe("URL codec", () => {
  it("round-trips custom filters including advanced backend names", () => {
    const filters = normalizeCorpusFilters({
      range: "custom",
      from: "2026-09-01",
      to: "2026-09-21",
      q: "audio-",
      status: "annotated",
      source_scene: "airport",
      source_confidence: "high",
      batch_code: "batch-1",
      review_status: "confirmed",
      prediction_scene: "airport",
      human_scene: "shopping",
    });
    const search = encodeCorpusUrlSearch(filters);
    expect(search).toContain("view=corpus");
    const decoded = decodeCorpusUrlSearch(search, "2026-09-21");
    expect(decoded).toEqual(filters);
  });

  it("keeps from/to in the URL only for custom ranges", () => {
    const preset = encodeCorpusUrlSearch(
      normalizeCorpusFilters({ range: "30d", from: "2026-08-23", to: "2026-09-21" }),
    );
    expect(preset).toContain("range=30d");
    expect(preset).not.toContain("from=");
    const decoded = decodeCorpusUrlSearch(preset, "2026-09-21");
    expect(decoded.from).toBe("2026-08-23");
    expect(decoded.to).toBe("2026-09-21");
  });
});

describe("query keys and response attribution", () => {
  it("keys contain role, session, and every normalized filter", () => {
    const filters = normalizeCorpusFilters({
      range: "custom",
      from: "2026-09-01",
      to: "2026-09-21",
      q: "audio",
      status: "annotated",
      source_scene: "airport",
    });
    const key = buildCorpusQueryKey({
      role: "admin",
      sessionKey: "primary",
      filters,
    });
    expect(key[0]).toBe("admin");
    expect(key[1]).toBe("corpus");
    expect(key[2]).toBe("admin");
    expect(key[3]).toBe("primary");
    expect(key[4]).toEqual(filters);
  });

  it("treats changed filters or sessions as different queries", () => {
    const base = normalizeCorpusFilters({ q: "a" });
    const same = buildCorpusQueryKey({ sessionKey: "k", filters: base });
    const again = buildCorpusQueryKey({
      sessionKey: "k",
      filters: normalizeCorpusFilters({ q: "a" }),
    });
    expect(again).toEqual(same);
    expect(
      buildCorpusQueryKey({ sessionKey: "k", filters: normalizeCorpusFilters({ q: "b" }) }),
    ).not.toEqual(same);
    expect(
      buildCorpusQueryKey({ sessionKey: "other", filters: base }),
    ).not.toEqual(same);
  });

  it("ignores late responses that no longer match the current request", () => {
    const current = makeCorpusRequestKey(normalizeCorpusFilters({ q: "new" }));
    const lateFirst = makeCorpusRequestKey(normalizeCorpusFilters({ q: "old" }));
    expect(isStaleCorpusResponse(lateFirst, current)).toBe(true);
    expect(isStaleCorpusResponse(current, current)).toBe(false);
    const appendKey = makeCorpusRequestKey(
      normalizeCorpusFilters({ q: "new" }),
      "cursor-1",
    );
    expect(isStaleCorpusResponse(appendKey, current)).toBe(true);
  });
});

describe("retry rules", () => {
  it("never retries 401/403/400", () => {
    expect(isNonRetryableCorpusStatus(401)).toBe(true);
    expect(isNonRetryableCorpusStatus(403)).toBe(true);
    expect(isNonRetryableCorpusStatus(400)).toBe(true);
    expect(isNonRetryableCorpusStatus(500)).toBe(false);
    expect(isNonRetryableCorpusStatus(undefined)).toBe(false);
    expect(shouldRetryCorpusRequest(0, { status: 401 })).toBe(false);
    expect(shouldRetryCorpusRequest(0, { status: 400 })).toBe(false);
  });

  it("retries transient failures only a bounded number of times", () => {
    expect(shouldRetryCorpusRequest(0, { status: 500 })).toBe(true);
    expect(shouldRetryCorpusRequest(1, new Error("network down"))).toBe(true);
    expect(shouldRetryCorpusRequest(2, { status: 500 })).toBe(false);
  });
});

describe("admin filter navigation", () => {
  it("keeps scene OR selections and assignee across requests and reloads", () => {
    const filters = normalizeCorpusFilters({ range: "all", source_scene: "clinic,airport,clinic", assignee_id: "unassigned", source_confidence: "high" });
    const params = buildCorpusApiParams(filters);
    expect(params.source_scene).toBe("airport,clinic");
    expect(params.assignee_id).toBe("unassigned");
    const restored = decodeCorpusUrlSearch(encodeCorpusUrlSearch(filters), "2026-09-30");
    expect(restored.source_scene).toBe("airport,clinic");
    expect(restored.assignee_id).toBe("unassigned");
    expect(restored.source_confidence).toBe("high");
  });
  it("uses the Shanghai calendar for both controls and dashboard requests near UTC midnight", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-09-29T22:30:00Z"));
      expect(filterToday()).toBe("2026-09-30");
      expect(dashboardDates(new URLSearchParams("range=7d"))).toEqual({ from: "2026-09-24", to: "2026-10-01", timezone: "Asia/Shanghai", bucket: "day" });
    } finally { vi.useRealTimers(); }
  });
});
