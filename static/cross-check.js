/* Shared cross-check labels, rate formatting, and Unicode-safe diff highlighting. */
(function (root) {
  "use strict";

  const STATE_LABELS = {
    in_progress: "In progress",
    awaiting_review: "Awaiting review",
    passed: "Passed",
    adjudicated: "Adjudicated",
    cancelled: "Cancelled",
    invalidated: "Invalidated",
  };
  const OPEN_STATES = { in_progress: true, awaiting_review: true };
  const REASON_LABELS = {
    word_difference_exceeded: "Word difference exceeds 10%",
    submission_status_conflict: "Submission status differs",
    empty_original_text: "Original transcript is empty",
    empty_secondary_text: "Cross-check transcript is empty",
    bad_quality_conflict: "Bad quality coverage differs",
    comparison_unavailable: "Comparison unavailable",
  };
  const OP_LABELS = {
    replace: "Replaced",
    insert: "Inserted",
    delete: "Deleted",
    overlap: "Difference",
    fallback: "Highlight unavailable",
  };
  const FLASH_KEY = "annotator.crossCheckFlash";
  const SKIP_REASONS = [
    ["noisy", "Noisy"],
    ["not_egyptian", "Not Egyptian"],
    ["poor_quality", "Poor quality"],
  ];

  function codePoints(text) {
    return Array.from(String(text == null ? "" : text));
  }

  function sliceCodePoints(text, start, end) {
    return codePoints(text).slice(start, end).join("");
  }

  function stateLabel(state) {
    return STATE_LABELS[state] || String(state || "Unknown");
  }

  function reasonLabel(code) {
    return REASON_LABELS[code] || String(code || "");
  }

  function reasonLabels(codes) {
    return (codes || []).map(reasonLabel).filter(Boolean);
  }

  function formatWordDifferenceRate(rate) {
    if (rate === null || rate === undefined || rate === "") return "Not available";
    const number = Number(rate);
    if (!Number.isFinite(number)) return "Not available";
    const percent = number * 100;
    const text = percent.toFixed(2).replace(/\.?0+$/, "");
    return text + "%";
  }

  function shortId(value) {
    const text = String(value || "");
    if (!text) return "—";
    return text.slice(0, 8);
  }

  function trainingExportBlocked(state) {
    return Boolean(OPEN_STATES[state]);
  }

  function samplingBpsFromPercent(raw) {
    const text = String(raw == null ? "" : raw).trim();
    if (!/^\d+(\.\d{1,2})?$/.test(text)) {
      throw new Error("Enter a percentage from 0 to 100 with up to two decimal places.");
    }
    const parts = text.split(".");
    const whole = Number(parts[0]);
    const frac = ((parts[1] || "") + "00").slice(0, 2);
    const bps = whole * 100 + Number(frac);
    if (bps < 0 || bps > 10000) {
      throw new Error("Sampling probability must be between 0% and 100%.");
    }
    return bps;
  }

  function percentFromBps(bps) {
    const number = Number(bps);
    if (!Number.isFinite(number)) return "";
    return String(Number((number / 100).toFixed(2)));
  }

  function trimmedReason(value) {
    return String(value == null ? "" : value).trim();
  }

  function reasonError(value) {
    const reason = trimmedReason(value);
    if (!reason) return "A reason is required.";
    if (Array.from(reason).length > 4000) return "Reason must be at most 4000 characters.";
    return "";
  }

  function segmentKey(segmentId) {
    return String(segmentId);
  }

  function segmentById(segments, segmentId) {
    const key = segmentKey(segmentId);
    return (segments || []).find((segment) => segmentKey(segment.id) === key) || null;
  }

  function mappingFor(op, side) {
    if (!op) return null;
    return side === "secondary" ? op.secondary : op.original;
  }

  function asOps(value) {
    if (Array.isArray(value)) return value;
    if (typeof value === "string") {
      try {
        const parsed = JSON.parse(value);
        return Array.isArray(parsed) ? parsed : [];
      } catch (_) {
        return [];
      }
    }
    return [];
  }

  function rangesBySegment(ops, side) {
    const map = new Map();
    asOps(ops).forEach((op, index) => {
      if (!op || op.op === "match") return;
      const mapping = mappingFor(op, side);
      if (!mapping) return;
      const key = segmentKey(mapping.segment_id);
      if (!map.has(key)) map.set(key, []);
      map.get(key).push({
        start: mapping.text_start,
        end: mapping.text_end,
        op: op.op,
        index: index,
        start_s: mapping.start_s,
        end_s: mapping.end_s,
      });
    });
    return map;
  }

  function highlightTranscript(container, rawText, ranges) {
    if (!container) return { fallback: false };
    container.replaceChildren();
    const points = codePoints(rawText);
    const n = points.length;
    const source = Array.isArray(ranges) ? ranges.slice() : [];
    let fallback = false;
    const valid = [];
    for (const range of source) {
      const start = Number(range && range.start);
      const end = Number(range && range.end);
      if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end > n || start > end) {
        fallback = true;
        continue;
      }
      valid.push({
        start: start,
        end: end,
        op: range.op || "replace",
        index: range.index,
      });
    }
    valid.sort(function (a, b) {
      return a.start - b.start || a.end - b.end || Number(a.index) - Number(b.index);
    });
    const placed = [];
    for (const range of valid) {
      const last = placed[placed.length - 1];
      if (last && range.start < last.end) {
        fallback = true;
        last.end = Math.max(last.end, range.end);
        last.op = "overlap";
        continue;
      }
      placed.push({
        start: range.start,
        end: range.end,
        op: range.op,
        index: range.index,
      });
    }
    if (fallback) {
      const mark = document.createElement("mark");
      mark.className = "cc-mark cc-mark-fallback";
      mark.dataset.op = "fallback";
      mark.setAttribute("data-diff-label", OP_LABELS.fallback);
      mark.textContent = points.join("");
      container.appendChild(mark);
      return { fallback: true };
    }
    let cursor = 0;
    for (const range of placed) {
      if (cursor < range.start) {
        container.appendChild(document.createTextNode(points.slice(cursor, range.start).join("")));
      }
      const mark = document.createElement("mark");
      mark.className = "cc-mark cc-mark-" + range.op;
      mark.dataset.op = range.op;
      mark.tabIndex = 0;
      if (range.index !== undefined && range.index !== null) mark.dataset.diffIndex = String(range.index);
      mark.setAttribute("data-diff-label", OP_LABELS[range.op] || "Difference");
      mark.textContent = points.slice(range.start, range.end).join("");
      container.appendChild(mark);
      cursor = range.end;
    }
    if (cursor < n) {
      container.appendChild(document.createTextNode(points.slice(cursor).join("")));
    }
    return { fallback: false };
  }

  function playableMapping(op) {
    if (!op) return null;
    const original = op.original && Number.isFinite(Number(op.original.start_s)) ? op.original : null;
    const secondary = op.secondary && Number.isFinite(Number(op.secondary.start_s)) ? op.secondary : null;
    return original || secondary;
  }

  function setSubmitFlash(payload) {
    try { sessionStorage.setItem(FLASH_KEY, JSON.stringify(payload || {})); }
    catch (_) { /* sessionStorage may be unavailable */ }
  }

  function consumeSubmitFlash() {
    try {
      const raw = sessionStorage.getItem(FLASH_KEY);
      if (!raw) return null;
      sessionStorage.removeItem(FLASH_KEY);
      return JSON.parse(raw);
    } catch (_) {
      return null;
    }
  }

  function submitMessage(result, fallbackStatus) {
    const info = result && result.cross_check;
    if (!info) {
      return fallbackStatus === "skipped" ? "Task skipped" : "Task completed";
    }
    if (info.state === "passed") return "Cross-check submitted — passed.";
    if (info.state === "awaiting_review") return "Cross-check submitted — awaiting admin review.";
    return "Cross-check submitted.";
  }

  function cloneJson(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function editorSegment(segment) {
    return {
      id: segment.id,
      start: segment.start,
      end: segment.end,
      duration: segment.duration,
      text: segment.text || "",
      exclude_from_training: Boolean(segment.exclude_from_training),
      asr_text: segment.asr_text || "",
    };
  }

  function decisionSegment(segment) {
    return {
      id: segment.id,
      start: segment.start,
      end: segment.end,
      duration: segment.duration,
      text: segment.text || "",
      exclude_from_training: Boolean(segment.exclude_from_training),
    };
  }

  root.CrossCheck = {
    STATE_LABELS: STATE_LABELS,
    REASON_LABELS: REASON_LABELS,
    OP_LABELS: OP_LABELS,
    OPEN_STATES: OPEN_STATES,
    SKIP_REASONS: SKIP_REASONS,
    FLASH_KEY: FLASH_KEY,
    codePoints: codePoints,
    sliceCodePoints: sliceCodePoints,
    stateLabel: stateLabel,
    reasonLabel: reasonLabel,
    reasonLabels: reasonLabels,
    formatWordDifferenceRate: formatWordDifferenceRate,
    shortId: shortId,
    trainingExportBlocked: trainingExportBlocked,
    samplingBpsFromPercent: samplingBpsFromPercent,
    percentFromBps: percentFromBps,
    trimmedReason: trimmedReason,
    reasonError: reasonError,
    segmentById: segmentById,
    asOps: asOps,
    rangesBySegment: rangesBySegment,
    highlightTranscript: highlightTranscript,
    playableMapping: playableMapping,
    setSubmitFlash: setSubmitFlash,
    consumeSubmitFlash: consumeSubmitFlash,
    submitMessage: submitMessage,
    cloneJson: cloneJson,
    editorSegment: editorSegment,
    decisionSegment: decisionSegment,
  };
})(window);
