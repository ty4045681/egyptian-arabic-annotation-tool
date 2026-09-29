import { describe, expect, it } from "vitest";
import { highlightParts, samplingBps } from "../features/admin/crossCheckModel";
import type { Segment } from "../features/admin/schemas";

describe("admin sampling and Unicode review", () => {
  it.each([
    ["0", 0],
    ["0.01", 1],
    ["10", 1000],
    ["12.50", 1250],
    ["100", 10000],
    ["100.01", null],
    ["0.001", null],
    ["-1", null],
    ["", null],
    ["NaN", null],
    ["1e2", null],
  ])("converts %s percent exactly", (input, expected) => {
    expect(samplingBps(String(input))).toBe(expected);
  });
  const segment: Segment = {
    id: 1,
    start: 0,
    end: 5,
    duration: 5,
    text: "e\u0301 😀 cat extra",
    exclude_from_training: false,
  };
  it("uses code-point offsets without splitting combining text or emoji", () => {
    expect(
      highlightParts(segment, "original", [
        {
          op: "replace",
          original: {
            segment_id: 1,
            text_start: 5,
            text_end: 8,
            start_s: 0,
            end_s: 5,
          },
          secondary: null,
        },
      ]),
    ).toEqual([
      { text: "e\u0301 😀 ", changed: false, index: null },
      { text: "cat", changed: true, index: 0 },
      { text: " extra", changed: false, index: null },
    ]);
  });
  it("preserves the whole transcript when a supplied span is invalid", () => {
    expect(
      highlightParts(segment, "original", [
        {
          op: "replace",
          original: {
            segment_id: 1,
            text_start: -1,
            text_end: 80,
            start_s: 0,
            end_s: 5,
          },
          secondary: null,
        },
      ]),
    ).toEqual([{ text: "e\u0301 😀 cat extra", changed: true, index: null }]);
  });
});
