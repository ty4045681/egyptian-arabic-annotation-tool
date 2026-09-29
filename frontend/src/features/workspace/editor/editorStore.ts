/**
 * Editor store (work package F, workspace/editor).
 *
 * Owns segment values, dirty ids, selection and scene-review state outside
 * React. Rows are keyed by stable segment id with per-row subscriptions, so
 * editing one row never re-renders the whole table. Text is stored verbatim:
 * no trim, no NFC normalization, no rewrites during IME composition. Remote
 * (server/restored) updates for a row that is mid-composition are deferred
 * until composition ends instead of yanking the caret.
 */

export interface SegmentValue {
  id: number | string;
  start: number;
  end: number;
  duration: number;
  text: string;
  asr_text?: string;
  exclude_from_training?: boolean;
  [field: string]: unknown;
}

export type SegmentPatch = Partial<SegmentValue>;

export interface AssignmentFingerprint {
  username: string;
  taskId: string;
  versionId: string;
  leaseToken: string;
  revision: number;
}

function sameJson(left: unknown, right: unknown): boolean {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}

function clone<T>(value: T): T {
  return value === undefined ? value : (JSON.parse(JSON.stringify(value)) as T);
}

export type SegmentListener = () => void;

interface RowState {
  value: SegmentValue;
  version: number;
  composing: boolean;
  pendingRemote: SegmentValue | null;
}

export interface EditorSnapshot {
  segments: SegmentValue[];
  dirtyIds: string[];
  selectedId: string | null;
  fingerprint: AssignmentFingerprint | null;
  generation: number;
}

const EMPTY_FINGERPRINT = "";

export class EditorStore {
  private rows = new Map<string, RowState>();
  private order: string[] = [];
  private dirty = new Set<string>();
  private selectedId: string | null = null;
  private fingerprint: AssignmentFingerprint | null = null;
  private generation = 0;
  private globalListeners = new Set<SegmentListener>();
  private rowListeners = new Map<string, Set<SegmentListener>>();
  /**
   * Pending time-field edits (X05): the raw text a user is typing, keyed by
   * segment id + field. While a field has a pending raw value, the domain
   * time is NOT updated (so intermediate/invalid strings never reach the
   * server) and external row-version changes must not overwrite the field.
   * A single commit path (blur/Enter/manual save) parses + clamps + applies.
   */
  private pendingTimes = new Map<string, { start?: string; end?: string }>();
  /**
   * Bounds listeners: notified only when start/end/duration change (never
   * for text-only edits). The media controller and waveform subscribe here
   * so edited playback boundaries stay authoritative without paying a
   * full-table re-render on every keystroke.
   */
  private boundsListeners = new Set<SegmentListener>();

  get currentGeneration(): number {
    return this.generation;
  }

  /** Load a new assignment context; drops all in-memory edits. */
  attachAssignment(
    fingerprint: AssignmentFingerprint,
    segments: SegmentValue[],
    options: { preserveSelection?: boolean } = {},
  ): void {
    this.generation += 1;
    this.fingerprint = { ...fingerprint };
    this.rows = new Map();
    this.order = [];
    this.rowListeners = new Map();
    for (const seg of segments) {
      const key = String(seg.id);
      this.order.push(key);
      this.rows.set(key, { value: clone(seg), version: 0, composing: false, pendingRemote: null });
    }
    this.dirty = new Set();
    if (!options.preserveSelection) this.selectedId = null;
    this.pendingTimes = new Map();
    this.emitGlobal();
    // Bounds listeners (media/waveform) must learn about the new snapshot;
    // a forced re-attach after conflict recovery changes times too (T04).
    this.emitBounds();
  }

  fingerprintKey(): string {
    if (!this.fingerprint) return EMPTY_FINGERPRINT;
    const f = this.fingerprint;
    return [f.username, f.taskId, f.versionId, f.leaseToken].join(":");
  }

  matchesFingerprint(fingerprint: AssignmentFingerprint): boolean {
    const current = this.fingerprint;
    return (
      Boolean(current) &&
      current !== null &&
      current.username === fingerprint.username &&
      current.taskId === fingerprint.taskId &&
      current.versionId === fingerprint.versionId &&
      current.leaseToken === fingerprint.leaseToken
    );
  }

  /** Local edit: stored verbatim, marks the row dirty. Never normalizes. */
  updateSegment(id: number | string, patch: SegmentPatch): void {
    const key = String(id);
    const row = this.rows.get(key);
    if (!row) return;
    const boundsChanged =
      (patch.start !== undefined && Number(patch.start) !== Number(row.value.start)) ||
      (patch.end !== undefined && Number(patch.end) !== Number(row.value.end)) ||
      (patch.duration !== undefined && Number(patch.duration) !== Number(row.value.duration));
    row.value = { ...row.value, ...clone(patch), id: row.value.id };
    row.version += 1;
    this.dirty.add(key);
    this.emitRow(key);
    if (boundsChanged) this.emitBounds();
  }

  /**
   * Remote update (server fetch, restored draft, conflict resolution).
   * Deferred while the row is mid-composition so the caret never jumps.
   */
  applyRemoteSegment(id: number | string, value: SegmentValue): void {
    const key = String(id);
    const row = this.rows.get(key);
    if (!row) return;
    if (row.composing) {
      row.pendingRemote = clone(value);
      return;
    }
    if (sameJson(row.value, value)) return;
    row.value = clone(value);
    row.version += 1;
    this.emitRow(key);
  }

  setComposing(id: number | string, composing: boolean): void {
    const key = String(id);
    const row = this.rows.get(key);
    if (!row || row.composing === composing) return;
    row.composing = composing;
    if (!composing && row.pendingRemote) {
      const pending = row.pendingRemote;
      row.pendingRemote = null;
      this.applyRemoteSegment(key, pending);
    }
  }

  isComposing(id: number | string): boolean {
    return this.rows.get(String(id))?.composing ?? false;
  }

  getSegment(id: number | string): SegmentValue | null {
    const row = this.rows.get(String(id));
    return row ? clone(row.value) : null;
  }

  rowVersion(id: number | string): number {
    return this.rows.get(String(id))?.version ?? -1;
  }

  isDirty(id: number | string): boolean {
    return this.dirty.has(String(id));
  }

  dirtyIds(): string[] {
    return this.order.filter((key) => this.dirty.has(key));
  }

  hasDirty(): boolean {
    return this.dirty.size > 0;
  }

  /**
   * Confirm semantics mirror AnnotationOffline.confirmOutbox(clear_dirty):
   * only rows whose current value still equals the sent snapshot leave the
   * dirty set; rows edited while the request was in flight stay dirty.
   */
  clearDirtyWhereEqual(sent: SegmentValue[]): string[] {
    const cleared: string[] = [];
    for (const snapshot of sent) {
      const key = String(snapshot.id);
      const row = this.rows.get(key);
      if (!row || !this.dirty.has(key)) continue;
      const current: Record<string, unknown> = {};
      const wanted: Record<string, unknown> = {};
      for (const field of Object.keys(snapshot)) {
        current[field] = (row.value as Record<string, unknown>)[field];
        wanted[field] = (snapshot as Record<string, unknown>)[field];
      }
      if (sameJson(current, wanted)) {
        this.dirty.delete(key);
        cleared.push(key);
      }
    }
    return cleared;
  }

  markAllClean(): void {
    this.dirty.clear();
  }

  // ---- pending time edits (X05) ----

  setPendingTime(id: number | string, field: "start" | "end", raw: string): void {
    const key = String(id);
    const entry = this.pendingTimes.get(key) ?? {};
    entry[field] = raw;
    this.pendingTimes.set(key, entry);
    // Y06: pending time edits are observable work. Notify subscribers so the
    // badge/Save button/leave guard reflect them without waiting for flush.
    this.emitGlobal();
  }

  clearPendingTime(id: number | string, field: "start" | "end"): void {
    const key = String(id);
    const entry = this.pendingTimes.get(key);
    if (!entry) return;
    delete entry[field];
    if (Object.keys(entry).length === 0) this.pendingTimes.delete(key);
    this.emitGlobal();
  }

  /** Whether a specific field currently has uncommitted raw text. */
  hasPendingTime(id: number | string, field: "start" | "end"): boolean {
    return this.pendingTimes.get(String(id))?.[field] !== undefined;
  }

  pendingTime(id: number | string, field: "start" | "end"): string | null {
    return this.pendingTimes.get(String(id))?.[field] ?? null;
  }

  hasPendingTimes(): boolean {
    return this.pendingTimes.size > 0;
  }

  /** Snapshot of all pending time text for the commit path. */
  pendingTimeEntries(): Array<{ id: string; field: "start" | "end"; raw: string }> {
    const out: Array<{ id: string; field: "start" | "end"; raw: string }> = [];
    for (const [id, entry] of this.pendingTimes) {
      if (entry.start !== undefined) out.push({ id, field: "start", raw: entry.start });
      if (entry.end !== undefined) out.push({ id, field: "end", raw: entry.end });
    }
    return out;
  }

  /**
   * Restore a stored working draft over freshly loaded server segments:
   * values are replaced wholesale (no caret to protect at entry) and only
   * the stored dirty ids stay dirty.
   */
  restoreFromDraft(segments: SegmentValue[], dirtyIds: string[]): void {
    const wanted = new Set(dirtyIds.map(String));
    for (const seg of segments) {
      const key = String(seg.id);
      const row = this.rows.get(key);
      if (!row) continue;
      row.value = clone(seg);
      row.version += 1;
      row.pendingRemote = null;
      this.emitRow(key);
    }
    this.dirty = new Set(this.order.filter((key) => wanted.has(key)));
  }

  select(id: number | string | null): void {
    const next = id === null ? null : String(id);
    if (this.selectedId === next) return;
    this.selectedId = next;
    this.emitGlobal();
  }

  get selected(): string | null {
    return this.selectedId;
  }

  subscribe(listener: SegmentListener): () => void {
    this.globalListeners.add(listener);
    return () => {
      this.globalListeners.delete(listener);
    };
  }

  /** Subscribe to start/end/duration changes only (playback bounds). */
  subscribeBounds(listener: SegmentListener): () => void {
    this.boundsListeners.add(listener);
    return () => {
      this.boundsListeners.delete(listener);
    };
  }

  subscribeSegment(id: number | string, listener: SegmentListener): () => void {
    const key = String(id);
    let set = this.rowListeners.get(key);
    if (!set) {
      set = new Set();
      this.rowListeners.set(key, set);
    }
    set.add(listener);
    return () => {
      set.delete(listener);
    };
  }

  getSnapshot(): EditorSnapshot {
    return {
      segments: this.order.map((key) => clone(this.rows.get(key)?.value as SegmentValue)),
      dirtyIds: this.dirtyIds(),
      selectedId: this.selectedId,
      fingerprint: this.fingerprint ? { ...this.fingerprint } : null,
      generation: this.generation,
    };
  }

  /** Payload for PATCH: only dirty rows plus any explicitly included ids. */
  dirtyPayload(extraIds: Array<number | string> = []): SegmentValue[] {
    const wanted = new Set<string>([...this.dirty, ...extraIds.map(String)]);
    return this.order.filter((key) => wanted.has(key)).map((key) => clone(this.rows.get(key)?.value as SegmentValue));
  }

  segmentCount(): number {
    return this.order.length;
  }

  private emitRow(key: string): void {
    this.rowListeners.get(key)?.forEach((listener) => {
      try {
        listener();
      } catch {
        // Listener errors must never corrupt store state.
      }
    });
  }

  private emitGlobal(): void {
    this.globalListeners.forEach((listener) => {
      try {
        listener();
      } catch {
        // Listener errors must never corrupt store state.
      }
    });
  }

  private emitBounds(): void {
    this.boundsListeners.forEach((listener) => {
      try {
        listener();
      } catch {
        // Listener errors must never corrupt store state.
      }
    });
  }
}

// ---- Transcript font-size preference (legacy localStorage key) ----

export const TRANSCRIPT_FONT_KEY = "annotator.transcriptFontSize";
export const TRANSCRIPT_FONT_DEFAULT = 18;
export const TRANSCRIPT_FONT_MIN = 14;
export const TRANSCRIPT_FONT_MAX = 32;
export const TRANSCRIPT_FONT_STEP = 2;

export function clampFontSize(value: unknown): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return TRANSCRIPT_FONT_DEFAULT;
  return Math.min(TRANSCRIPT_FONT_MAX, Math.max(TRANSCRIPT_FONT_MIN, Math.round(n)));
}

export function loadTranscriptFontSize(storage?: {
  getItem: (key: string) => string | null;
}): number {
  try {
    const source = storage ?? globalThis.localStorage;
    return clampFontSize(source.getItem(TRANSCRIPT_FONT_KEY) ?? TRANSCRIPT_FONT_DEFAULT);
  } catch {
    return TRANSCRIPT_FONT_DEFAULT;
  }
}

export function saveTranscriptFontSize(
  size: number,
  storage?: { setItem: (key: string, value: string) => void },
): number {
  const clamped = clampFontSize(size);
  try {
    (storage ?? globalThis.localStorage).setItem(TRANSCRIPT_FONT_KEY, String(clamped));
  } catch {
    // Preference loss is non-fatal; the caller keeps the in-memory size.
  }
  return clamped;
}
