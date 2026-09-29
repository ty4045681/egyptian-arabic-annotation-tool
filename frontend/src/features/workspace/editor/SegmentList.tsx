import { memo, useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { EditorStore, SegmentPatch } from "./editorStore";
import { clampTimeEdit, formatTime, parseTimeInput } from "./segmentTime";
import type { MediaController } from "../media/mediaController";
import styles from "./SegmentList.module.css";

/**
 * Row writes go through `onUpdateSegment` (the persistence controller),
 * never `store.updateSegment` directly: only the controller notes queue
 * input, persists locally, schedules autosave and emits snapshots. A
 * direct store write would dirty the editor while the save status, queue
 * and autosave stay silent.
 */
export type SegmentUpdateHandler = (id: string, patch: SegmentPatch) => void;

interface RowProps {
  segmentId: string;
  rowIndex: number;
  store: EditorStore;
  media: MediaController;
  disabled: boolean;
  onUpdateSegment: SegmentUpdateHandler;
  /**
   * Real audio duration: bounds the last segment's end (T05). Mirrors the
   * legacy page's `bounds()` and the timeline validation the server applies
   * on submit.
   */
  duration: number;
}

function useRowVersion(store: EditorStore, segmentId: string): number {
  const [version, setVersion] = useState(() => store.rowVersion(segmentId));
  useEffect(
    () =>
      store.subscribeSegment(segmentId, () => {
        if (store.isComposing(segmentId)) return;
        setVersion(store.rowVersion(segmentId));
      }),
    [store, segmentId],
  );
  return version;
}

function usePlaybackMembership(
  media: MediaController,
  rowIndex: number,
  bounds: { start: number; end: number },
): {
  playing: boolean;
  selected: boolean;
  /** Elapsed seconds WITHIN this segment (X06); 0 unless active/selected. */
  segmentElapsed: number;
} {
  const startRef = useRef(bounds.start);
  const endRef = useRef(bounds.end);
  startRef.current = bounds.start;
  endRef.current = bounds.end;
  const [membership, setMembership] = useState(() => {
    const snapshot = media.getSnapshot();
    return {
      playing: snapshot.playingIndex === rowIndex && snapshot.playing,
      selected: snapshot.selectedIndex === rowIndex,
      segmentElapsed: 0,
    };
  });
  useEffect(
    () =>
      media.subscribe((cursor) => {
        setMembership((previous) => {
          const active = cursor.playingIndex === rowIndex || cursor.selectedIndex === rowIndex;
          // X06: segment-relative, clamped to this segment's own 0..length.
          const length = Math.max(0, endRef.current - startRef.current);
          const elapsed = active
            ? Math.max(0, Math.min(cursor.currentTime - startRef.current, length))
            : 0;
          const next = {
            playing: cursor.playingIndex === rowIndex && cursor.playing,
            selected: cursor.selectedIndex === rowIndex,
            segmentElapsed: elapsed,
          };
          return previous.playing === next.playing &&
            previous.selected === next.selected &&
            Math.abs(previous.segmentElapsed - next.segmentElapsed) < 0.05
            ? previous
            : next;
        });
      }),
    [media, rowIndex],
  );
  // Bounds may change via an edit; recompute once directly.
  useEffect(() => {
    setMembership((previous) => {
      const snapshot = media.getSnapshot();
      const active = snapshot.playingIndex === rowIndex || snapshot.selectedIndex === rowIndex;
      const length = Math.max(0, bounds.end - bounds.start);
      const elapsed = active
        ? Math.max(0, Math.min(snapshot.currentTime - bounds.start, length))
        : 0;
      return Math.abs(previous.segmentElapsed - elapsed) < 0.05
        ? previous
        : { ...previous, segmentElapsed: elapsed };
    });
  }, [media, rowIndex, bounds.start, bounds.end]);
  return membership;
}

function TranscriptRowInner({ segmentId, rowIndex, store, media, disabled, onUpdateSegment, duration }: RowProps): React.JSX.Element {
  const version = useRowVersion(store, segmentId);
  const segment = store.getSegment(segmentId);
  const membership = usePlaybackMembership(media, rowIndex, {
    start: Number(segment?.start ?? 0),
    end: Number(segment?.end ?? 0),
  });
  const [text, setText] = useState(() => segment?.text ?? "");
  // AA03: initialize from restored raw time text if present, otherwise the
  // formatted domain value.
  const [startInput, setStartInput] = useState(
    () => store.pendingTime(segmentId, "start") ?? formatTime(Number(segment?.start ?? 0)),
  );
  const [endInput, setEndInput] = useState(
    () => store.pendingTime(segmentId, "end") ?? formatTime(Number(segment?.end ?? 0)),
  );
  const [timeError, setTimeError] = useState<"start" | "end" | null>(() => {
    const pendingStart = store.pendingTime(segmentId, "start");
    const pendingEnd = store.pendingTime(segmentId, "end");
    if (pendingStart !== null && parseTimeInput(pendingStart) === null) return "start";
    if (pendingEnd !== null && parseTimeInput(pendingEnd) === null) return "end";
    return null;
  });
  const areaRef = useRef<HTMLTextAreaElement | null>(null);
  /**
   * Set by cancelTime() so the programmatic blur it triggers does not
   * re-commit/re-error the pre-cancel DOM value (AB01/AB04).
   */
  const suppressBlurRef = useRef<"start" | "end" | null>(null);

  useEffect(() => {
    const current = store.getSegment(segmentId);
    if (current && !store.isComposing(segmentId)) {
      setText(current.text ?? "");
      // X05: never overwrite a field the user is currently editing.
      if (store.pendingTime(segmentId, "start") === null) {
        setStartInput(formatTime(Number(current.start ?? 0)));
      }
      if (store.pendingTime(segmentId, "end") === null) {
        setEndInput(formatTime(Number(current.end ?? 0)));
      }
    }
  }, [version, store, segmentId]);

  const commitText = useCallback(
    (value: string) => {
      if (disabled) return;
      onUpdateSegment(segmentId, { text: value });
    },
    [disabled, onUpdateSegment, segmentId],
  );

  const exclusions = segment?.exclude_from_training === true;

  const commitTime = useCallback(
    (field: "start" | "end", raw: string): boolean => {
      const current = store.getSegment(segmentId);
      if (!current || disabled) {
        return false;
      }
      const parsed = parseTimeInput(raw);
      if (parsed === null) {
        return false;
      }
      const snapshot = store.getSnapshot().segments;
      const min = rowIndex > 0 ? Number(snapshot[rowIndex - 1]?.end ?? 0) : 0;
      // Last segment is bounded by the real audio duration (T05), not
      // Number.MAX_SAFE_INTEGER; the server rejects over-length ends.
      const max =
        rowIndex < snapshot.length - 1
          ? Number(snapshot[rowIndex + 1]?.start ?? parsed)
          : (Number.isFinite(duration) && duration > 0 ? duration : parsed);
      const next = clampTimeEdit(field, parsed, current, { min, max });
      const end = field === "start" ? Number(current.end) : next;
      const start = field === "start" ? next : Number(current.start);
      store.clearPendingTime(segmentId, field);
      onUpdateSegment(segmentId, {
        [field]: next,
        duration: Math.round((end - start) * 1000) / 1000,
      });
      return true;
    },
    [disabled, duration, onUpdateSegment, rowIndex, store, segmentId],
  );

  /**
   * Time edit handler (X05). Typing only updates the LOCAL raw text and the
   * store's pending registry; the domain time is not touched, so a
   * half-typed value can never reach the server and the caret never moves.
   * A single commit (blur/Enter, or the controller's pending-edit commit for
   * Save/Ctrl+S/autosave) parses + validates + applies.
   */
  const changeTime = useCallback(
    (field: "start" | "end", raw: string) => {
      if (field === "start") setStartInput(raw);
      else setEndInput(raw);
      store.setPendingTime(segmentId, field, raw);
      setTimeError(parseTimeInput(raw) === null ? field : null);
    },
    [store, segmentId],
  );

  const blurTime = useCallback(
    (field: "start" | "end", raw: string) => {
      // AB01/AB04: an Escape cancel triggers a programmatic blur whose DOM
      // value still holds the pre-cancel text. That blur must NOT re-commit
      // or re-set the error.
      if (suppressBlurRef.current === field) {
        suppressBlurRef.current = null;
        return;
      }
      // Y07: on blur, commit a valid value; keep an invalid one in the field
      // WITH its error so the user sees what Save did not accept. Only an
      // explicit cancel (Escape) restores the stored value.
      if (commitTime(field, raw)) {
        setTimeError(null);
        return;
      }
      setTimeError(parseTimeInput(raw) === null ? field : null);
    },
    [commitTime],
  );

  const cancelTime = useCallback(
    (field: "start" | "end") => {
      // AB01/AB04: cancel is its own action. Mark the field so the blur it
      // provokes is ignored, discard the raw text, restore the committed
      // value, and clear the error — all synchronously.
      suppressBlurRef.current = field;
      store.clearPendingTime(segmentId, field);
      const current = store.getSegment(segmentId);
      if (current) {
        if (field === "start") setStartInput(formatTime(Number(current.start)));
        else setEndInput(formatTime(Number(current.end)));
      }
      setTimeError(null);
    },
    [store, segmentId],
  );

  const onTimeKey = useCallback(
    (event: KeyboardEvent<HTMLInputElement>, field: "start" | "end") => {
      if (event.key === "Enter") {
        // Enter commits a complete value; an invalid one stays visible.
        if (commitTime(field, event.currentTarget.value)) setTimeError(null);
        else setTimeError(parseTimeInput(event.currentTarget.value) === null ? field : null);
        event.currentTarget.blur();
      } else if (event.key === "Escape") {
        cancelTime(field);
        event.currentTarget.blur();
      }
    },
    [cancelTime, commitTime],
  );

  const toggleQuality = useCallback(() => {
    const current = store.getSegment(segmentId);
    if (!current || disabled) return;
    onUpdateSegment(segmentId, {
      exclude_from_training: !(current.exclude_from_training === true),
    });
  }, [disabled, onUpdateSegment, store, segmentId]);

  const playRow = useCallback(() => {
    const snapshot = media.getSnapshot();
    if (snapshot.playingIndex === rowIndex && snapshot.playing) {
      media.pause();
      return;
    }
    media.playSegment(rowIndex);
  }, [media, rowIndex]);

  const dirty = store.isDirty(segmentId);
  const displayId = segment?.id ?? rowIndex + 1;

  return (
    <article
      aria-label={`Segment ${String(displayId)}`}
      data-segment-row={segmentId}
      data-playing={membership.playing ? "true" : "false"}
      data-selected={membership.selected ? "true" : "false"}
      className={`${styles.row} ${membership.selected ? styles.selected : ""} ${dirty ? styles.dirty : ""}`}
    >
      <div className={styles.meta}>
        <button
          type="button"
          onClick={playRow}
          disabled={disabled}
          aria-pressed={membership.playing}
          aria-label={`${membership.playing ? "Pause" : "Play"} segment ${String(displayId)}`}
          data-play={rowIndex}
          className={styles.playButton}
        >
          {membership.playing ? "⏸" : "▶"}
        </button>
        <span className={styles.index} dir="ltr">
          <bdi>#{String(displayId)}</bdi>
        </span>
        <label className={styles.timeField}>
          <span className={styles.timeLabel}>Start</span>
          <input
            dir="ltr"
            value={startInput}
            disabled={disabled}
            aria-label={`Segment ${String(displayId)} start`}
            aria-invalid={timeError === "start"}
            data-time="start"
            data-index={rowIndex}
            onChange={(event) => changeTime("start", event.target.value)}
            onBlur={(event) => blurTime("start", event.target.value)}
            onKeyDown={(event) => onTimeKey(event, "start")}
            className={`${styles.timeInput} ${timeError === "start" ? styles.timeInvalid : ""}`}
            inputMode="decimal"
          />
        </label>
        <label className={styles.timeField}>
          <span className={styles.timeLabel}>End</span>
          <input
            dir="ltr"
            value={endInput}
            disabled={disabled}
            aria-label={`Segment ${String(displayId)} end`}
            aria-invalid={timeError === "end"}
            data-time="end"
            data-index={rowIndex}
            onChange={(event) => changeTime("end", event.target.value)}
            onBlur={(event) => blurTime("end", event.target.value)}
            onKeyDown={(event) => onTimeKey(event, "end")}
            className={`${styles.timeInput} ${timeError === "end" ? styles.timeInvalid : ""}`}
            inputMode="decimal"
          />
        </label>
        <span className={styles.clock} dir="ltr" data-clock={rowIndex}>
          {formatTime(membership.segmentElapsed).slice(0, 5)}
        </span>
        <button
          type="button"
          onClick={toggleQuality}
          disabled={disabled}
          aria-pressed={exclusions}
          aria-label={`Mark segment ${String(displayId)} as bad quality`}
          data-quality={rowIndex}
          className={`${styles.qualityButton} ${exclusions ? styles.excluded : ""}`}
        >
          Bad quality
        </button>
      </div>
      <textarea
        ref={areaRef}
        dir="auto"
        value={text}
        disabled={disabled}
        rows={2}
        aria-label={`Segment ${String(displayId)} transcript`}
        data-text={rowIndex}
        data-transcript={rowIndex}
        data-segment-id={segmentId}
        placeholder={typeof segment?.asr_text === "string" && segment.asr_text ? segment.asr_text : "Enter Arabic transcript"}
        onChange={(event) => {
          setText(event.target.value);
          commitText(event.target.value);
        }}
        onCompositionStart={() => store.setComposing(segmentId, true)}
        onCompositionEnd={(event) => {
          store.setComposing(segmentId, false);
          const current = store.getSegment(segmentId);
          if (current && current.text !== event.currentTarget.value) {
            setText(current.text ?? "");
          }
        }}
        onFocus={() => {
          store.select(segmentId);
          media.select(rowIndex);
        }}
        className={styles.transcript}
      />
    </article>
  );
}

export const TranscriptRow = memo(TranscriptRowInner);

export function SegmentList({
  store,
  media,
  disabled,
  onUpdateSegment,
  duration,
}: {
  store: EditorStore;
  media: MediaController;
  disabled: boolean;
  onUpdateSegment: SegmentUpdateHandler;
  duration: number;
}): React.JSX.Element {
  const [order, setOrder] = useState<string[]>(() => store.getSnapshot().segments.map((seg) => String(seg.id)));
  useEffect(
    () =>
      store.subscribe(() => {
        setOrder(store.getSnapshot().segments.map((seg) => String(seg.id)));
      }),
    [store],
  );
  if (order.length === 0) {
    return (
      <div role="status" className={styles.empty}>
        This task has no speech segments and cannot be annotated.
      </div>
    );
  }
  return (
    <div data-testid="segment-list">
      {order.map((segmentId, rowIndex) => (
        <TranscriptRow
          key={segmentId}
          segmentId={segmentId}
          rowIndex={rowIndex}
          store={store}
          media={media}
          disabled={disabled}
          onUpdateSegment={onUpdateSegment}
          duration={duration}
        />
      ))}
    </div>
  );
}
