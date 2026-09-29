/**
 * Persistence / session controller (work package F, workspace/persistence).
 *
 * Lives outside page components and owns the serial save queue, IndexedDB
 * drafts/outbox, activity heartbeat and freeze handling. Lifecycle order is
 * invariant: persist locally first, then stage the frozen outbox request,
 * then send the immutable PATCH body.
 *
 * Controller surface: attachSession / attachAssignment / subscribe /
 * getSnapshot / updateSegment / updateReview / persistLocal / flush /
 * freeze / handleOnline / discardLocalConflict / exportLocalText / dispose.
 */

import { ApiError, classifyFailure } from "../../auth/sessionErrors";
import { SessionService, type FreezeCode, type SessionConfig } from "../../auth/sessionService";
import { EditorStore, type AssignmentFingerprint, type SegmentPatch } from "../editor/editorStore";
import { clampTimeEdit, parseTimeInput } from "../editor/segmentTime";
import {
  cloneReview,
  reviewsEqual,
  reviewWriteEnabled,
  shouldSaveReview,
  type FeatureFlags,
  type SceneReviewValue,
} from "../editor/sceneReview";
import { LegacyOfflineAdapter, validateStoredDraft, type OfflineStore } from "./offlineAdapter";
import { replaySaveOutbox, type ReplayOutcome } from "./outboxReplay";import { SaveQueue, type QueuedOperation } from "./saveQueue";
import {
  SAVE_METHOD,
  SAVE_ROUTE,
  isTerminalOutboxRoute,
  type FreezeReason,
  type OutboxRecord,
  type SaveRequestBody,
  type SaveResult,
  type SaveStatus,
} from "./types";

export interface AssignmentContext extends AssignmentFingerprint {
  segments: Array<Record<string, unknown>>;
  sceneReview: SceneReviewValue | null;
  features: FeatureFlags;
  /** Real audio duration; bounds the last segment's end (T05/X05). */
  duration: number;
  mode?: string;
  roundId?: string;
}

export interface ConflictInfo {
  kind: "lease-mismatch" | "revision-mismatch" | "assignment-invalid";
  localRevision: number | null;
  serverRevision: number | null;
}

export interface EntryReport {
  checked: boolean;
  storedDraft: boolean;
  pendingOutbox: number;
  terminalOutbox: OutboxRecord[];
  conflict: ConflictInfo | null;
  replayed: number;
  storageHealth: string;
}

export interface ControllerSnapshot {
  status: SaveStatus;
  ready: boolean;
  revision: number | null;
  dirtyCount: number;
  reviewDirty: boolean;
  /** Current scene-review value (cloned); drives the collapsed review form. */
  review: SceneReviewValue | null;
  pendingCount: number;
  frozenReason: FreezeReason | null;
  conflict: ConflictInfo | null;
  terminalOutbox: OutboxRecord[];
  routeToLegacy: boolean;
  needsRefetch: boolean;
  error: string | null;
  storageHealth: string;
  hasAssignment: boolean;
  /**
   * Recovery gate: a conflict recovery is in progress or failed and the
   * controller is refusing all writes until the authoritative assignment is
   * re-attached (T02).
   */
  recoveryPending: boolean;
  /**
   * Active storage barrier (batch 1). Non-null blocks all writes until the
   * user resolves it; see the controller field for the kinds.
   */
  storageBarrier: string | null;
  /** Human-readable explanation for the active barrier. */
  storageBarrierMessage: string | null;
  /** Local backup is failing even if the server sync succeeded (V02). */
  localWriteFailed: boolean;
  /**
   * IDs of accepted-but-unconfirmed operations (Y01). Any non-empty set keeps
   * the page out of "Saved"; a different operation's success cannot clear it.
   */
  unconfirmedOperationIds: string[];
  /** Uncommitted time-field edits (Y06): observable unsaved work. */
  pendingTimeCount: number;
  /** Feature flags for the active assignment (W05). */
  features: FeatureFlags;
  /** A confirmed older replay is followed by newer queued input (W03). */
  followUpPending: boolean;
  /**
   * Editor generation: bumps on every attachAssignment. Row components use
   * it as a React key so a forced re-attach (e.g. after discard) remounts
   * rows with the authoritative snapshot instead of keeping stale local
   * text/time state (N03).
   */
  generation: number;
}

export interface Sender {
  (body: SaveRequestBody): Promise<SaveResult>;
}

export interface ControllerDeps {
  store?: OfflineStore;
  sender: Sender;
  sessionFactory?: (hooks: {
    persist: () => Promise<void>;
    hasDirty: () => boolean;
    onFrozen: (code: FreezeCode) => void;
    onOnline: () => Promise<void>;
  }) => SessionService;
  autosaveDelayMs?: number;
  persistDelayMs?: number;
  setTimeout?: (callback: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimeout?: (handle: ReturnType<typeof setTimeout>) => void;
  newOperationId?: () => string;
}

type Listener = (snapshot: ControllerSnapshot) => void;

const AUTOSAVE_DEFAULT_MS = 3000;
const PERSIST_DEFAULT_MS = 400;

/** Keys hidden from any user-visible export (blind-marking metadata). */
const BLIND_EXPORT_KEYS = new Set(["mode", "round_id", "roundId", "roundID"]);

/**
 * Recursively remove blind-marking metadata from an export object before it
 * is shown or copied to the annotator (V01). The legacy `exportText` only
 * stripped top-level keys, so a nested preserved draft leaked `mode` and the
 * review round id. This walks arrays and plain objects.
 */
function sanitizeForExport(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sanitizeForExport);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
      if (BLIND_EXPORT_KEYS.has(key)) continue;
      out[key] = sanitizeForExport(entry);
    }
    return out;
  }
  return value;
}

function genericErrorMessage(status: number, code?: string): string {
  if (status === 0) return "Network connection failed. Edits stay on this device.";
  if (status === 401) return "Session expired. Local edits are kept on this device.";
  if (status === 403 && code === "account_deactivated") {
    return "Account deactivated. This page can no longer save.";
  }
  if (status === 409) return "This task changed elsewhere. Local edits are kept for review.";
  if (status >= 500) return "Server error. Local edits are kept; retry shortly.";
  return "Save failed. Local edits are kept on this device.";
}

export class PersistenceController {
  readonly editor = new EditorStore();
  private store: OfflineStore;
  private sender: Sender;
  private sessionFactory: ControllerDeps["sessionFactory"];
  private session: SessionService | null = null;
  private queue: SaveQueue;
  private context: AssignmentContext | null = null;
  private generation = 0;
  private revision: number | null = null;
  private review: SceneReviewValue | null = null;
  private reviewDirty = false;
  private ready = false;
  private conflict: ConflictInfo | null = null;
  private terminalOutbox: OutboxRecord[] = [];
  private routeToLegacy = false;
  private needsRefetch = false;
  private frozenReason: FreezeReason | null = null;
  private error: string | null = null;
  private lastStoredDraft: Record<string, unknown> | null = null;
  /**
   * True when `conflict` reflects an unresolved draft preserved from an
   * earlier lease/version discovered at entry: the stored record is NOT the
   * current editor's content, so `persistLocal` must not overwrite it
   * (N01). A conflict raised by the current editor's own in-flight save
   * leaves this false so the newest input is still persisted before freeze
   * (T01).
   */
  private preservedDraftConflict = false;
  /**
   * Single serial-replay mutex (U02): startup, online and any concurrent
   * online handler share ONE replay promise. A second caller awaits the
   * same promise instead of starting a second outbox replay, so a late
   * confirmation can never rewind a revision a parallel replay advanced.
   */
  private replayInFlight: Promise<void> | null = null;
  /**
   * True while a conflict recovery is in progress or failed: no write path
   * may send and no local snapshot may be re-persisted (T02/U03).
   */  private recoveryPending = false;
  /**
   * Storage state machine (batch 1). A non-null barrier blocks ALL writes
   * (local and network) until the user resolves it, so unknown/old data is
   * never silently overwritten or bypassed:
   * - "draft-read-error": the current draft read failed (W08)
   * - "corrupt-draft": a corrupt record exists at this key (V03)
   * - "corrupt-outbox": a corrupt persisted operation blocks new saves (V04)
   * - "storage-error": local persistence is failing (quota etc.) (V02)
   * - "replay-failed": an older operation could not be replayed yet (W03)
   */
  private storageBarrier: string | null = null;
  /**
   * Local-write failure indicator. Independent from the server sync state so
   * a successful PATCH cannot mask a still-failing local backup (V02).
   */
  private localWriteFailed = false;
  /**
   * Accepted-but-unconfirmed operations by ID (Y01/Y02). A confirmation only
   * removes ITS OWN id; another operation's success cannot clear the fault.
   */
  private unconfirmedIds = new Set<string>();
  private retentionApplied = false;
  private followUpPending = false;
  /**
   * AB02: true while a recovery preflight (confirm unconfirmed → replay the
   * persisted outbox) owns the writer. `onQueueChanged` must not auto-enqueue
   * newer input during this window, or B would overtake a persisted X.
   */
  private preflightActive = false;
  /** Explicit entry retention window (X04); null until set by the caller. */
  private entryRetentionDays: number | null = null;
  private autosaveDelay: number;
  private persistDelay: number;
  private setTimeoutImpl: Required<ControllerDeps>["setTimeout"];
  private clearTimeoutImpl: Required<ControllerDeps>["clearTimeout"];
  private persistTimer: ReturnType<typeof setTimeout> | null = null;
  private autosaveTimer: ReturnType<typeof setTimeout> | null = null;
  private listeners = new Set<Listener>();
  private disposed = false;

  constructor(deps: ControllerDeps) {
    this.store = deps.store ?? new LegacyOfflineAdapter();
    this.sender = deps.sender;
    this.sessionFactory = deps.sessionFactory;
    this.autosaveDelay = deps.autosaveDelayMs ?? AUTOSAVE_DEFAULT_MS;
    this.persistDelay = deps.persistDelayMs ?? PERSIST_DEFAULT_MS;
    this.setTimeoutImpl = deps.setTimeout ?? ((callback, ms) => globalThis.setTimeout(callback, ms));
    this.clearTimeoutImpl = deps.clearTimeout ?? ((handle) => globalThis.clearTimeout(handle));
    this.queue = new SaveQueue(
      {
        stageOutbox: (op) => this.stageOutbox(op),
        send: (body) => this.sendSave(body),
        confirm: (op, result) => this.confirmOperation(op, result),
        bumpAttempt: (op) => this.bumpAttempt(op),
      },
      { ...(deps.newOperationId !== undefined ? { newOperationId: deps.newOperationId } : {}) },
    );
    this.queue.subscribe(() => {
      this.onQueueChanged();
    });
    // Y06/AA03: uncommitted time-field text is observable unsaved work;
    // re-emit the controller snapshot AND schedule the same deferred local
    // persistence as a text edit, so a time-only change is recoverable.
    this.editor.subscribe(() => {
      if (this.editor.hasPendingTimes()) void this.persistSoon();
      this.emit();
    });
  }

  // ---- session ----

  attachSession(session: Partial<SessionConfig> | null | undefined, username: string): void {
    void username;
    if (!this.sessionFactory) return;
    this.session?.dispose();
    this.session = this.sessionFactory({
      persist: async () => {
        await this.persistLocal();
      },
      hasDirty: () => this.hasUnsavedWork(),
      onFrozen: (code) => this.handleSessionFrozen(code),
      onOnline: () => this.handleOnline(),
    });
    void session;
  }

  get sessionService(): SessionService | null {
    return this.session;
  }

  /** Draft retention window (days); from session config, default 7 (W04). */
  private get retentionDays(): number {
    // X04: an explicit entry value (from the just-fetched /api/current-user)
    // wins over the session object, which may not be attached yet.
    if (this.entryRetentionDays !== null) return this.entryRetentionDays;
    const configured = this.session?.retentionDays;
    return typeof configured === "number" && configured > 0 ? configured : 7;
  }

  start(): void {
    this.session?.start();
    this.queue.start();
  }

  // ---- assignment ----

  /**
   * Single serial replay entry point (U02): startup and every online event
   * funnel through here. Concurrent callers await the same promise instead
   * of launching a second replay; the mutex is released in finally. Applies
   * confirmed revisions/dirty clearing and returns the outcome so callers
   * can react (conflict verdict, follow-up flush). Returns null for a
   * follower call (the owner already applied results and verdict).
   */
  private async runReplay(): Promise<ReplayOutcome | null> {
    if (!this.context) return null;
    if (this.replayInFlight) {
      await this.replayInFlight;
      return null;
    }
    const context = this.context;
    const generation = this.generation;
    const run: Promise<ReplayOutcome | null> = (async () => {
      await this.queue.suspendForReplay();
      const replayed = await replaySaveOutbox(context.username, context.taskId, {
        store: this.store,
        send: (body) => this.sendSave(body, { silent: true }),
        confirm: async (item, revision) => {
          await this.store.confirmOutbox(item.operation_id, {
            server_revision: revision,
            clear_dirty: true,
          });
          // Y02: a successful replay confirmation of A clears A's own
          // unconfirmed fact, on the same page, without a reload. Clear it on
          // the WRITER too, so its confirmation phase is released.
          this.unconfirmedIds.delete(item.operation_id);
          this.queue.markConfirmed(item.operation_id);
          if (this.unconfirmedIds.size === 0 && this.storageBarrier === null) {
            this.error = null;
          }
        },
      });
      if (this.disposed || generation !== this.generation) return null;
      this.applyReplayApplied(replayed.applied);
      // Z03: a successful replay read is the same-source evidence that clears
      // a transient read/replay barrier (and its derived error message).
      if (
        !replayed.readError &&
        !replayed.failed.length &&
        (this.storageBarrier === "replay-read-error" || this.storageBarrier === "replay-failed")
      ) {
        this.storageBarrier = null;
        if (this.unconfirmedIds.size === 0) this.error = null;
      }
      return replayed;
    })();
    this.replayInFlight = run.then(
      () => undefined,
      () => undefined,
    ).finally(() => {
      this.replayInFlight = null;
    });
    return run;
  }

  /**
   * Enter an assignment. Before edits/autosave are enabled, the controller
   * checks this user/task's stored draft and outbox: terminal outbox entries
   * are retained (never replayed as PATCH); lease/version/revision drift
   * produces an explicit conflict instead of an overwrite.
   */
  async attachAssignment(
    context: AssignmentContext,
    options: { force?: boolean; retentionDays?: number } = {},
  ): Promise<EntryReport> {
    // X04: the server retention window is an ENTRY INPUT, resolved before any
    // cleanup/read so it never depends on session-init order.
    if (typeof options.retentionDays === "number" && options.retentionDays > 0) {
      this.entryRetentionDays = options.retentionDays;
    }
    const generation = ++this.generation;
    const changed =
      options.force === true ||
      !this.context ||
      this.context.username !== context.username ||
      this.context.taskId !== context.taskId ||
      this.context.versionId !== context.versionId ||
      this.context.leaseToken !== context.leaseToken;
    if (changed) {
      this.queue.reset();
      this.editor.attachAssignment(
        {
          username: context.username,
          taskId: context.taskId,
          versionId: context.versionId,
          leaseToken: context.leaseToken,
          revision: context.revision,
        },
        context.segments.map((seg) => ({ ...(seg as object), text: String((seg as { text?: unknown }).text ?? "") }) as never),
      );
      this.review = cloneReview(context.sceneReview);
      this.reviewDirty = false;
      this.revision = context.revision;
      this.conflict = null;
      this.preservedDraftConflict = false;
      this.recoveryPending = false;
      this.storageBarrier = null;
      this.localWriteFailed = false;
      this.retentionApplied = false;
      this.terminalOutbox = [];
      this.routeToLegacy = false;
      this.needsRefetch = false;
      this.frozenReason = null;
      this.error = null;
      this.ready = false;
    } else if (this.revision !== context.revision) {
      this.revision = context.revision;
    }
    this.context = { ...context, segments: [...context.segments] };
    this.emit();

    const report: EntryReport = {
      checked: false,
      storedDraft: false,
      pendingOutbox: 0,
      terminalOutbox: [],
      conflict: null,
      replayed: 0,
      storageHealth: "ok",
    };
    try {
      // Retention sweep (W04): remove this user's expired drafts/outbox
      // before trusting anything. A failed sweep is a storage read failure:
      // do not proceed to restore/replay potentially-expired records.
      if (!this.retentionApplied) {
        try {
          await this.store.purgeExpired(this.retentionDays, context.username);
          this.retentionApplied = true;
        } catch {
          this.setStorageBarrier("draft-read-error");
          report.checked = true;
          this.ready = true;
          this.emit();
          return report;
        }
      }
      // Distinguishable reads (W08): a read error must NOT collapse to
      // "no draft/outbox" and let the next write overwrite unknown data.
      const draftRead = await this.store.readWorkingDraft(context.username, context.taskId);
      const outboxRead = await this.store.readOutbox(context.username);
      if (draftRead.status === "error" || outboxRead.status === "error") {
        this.setStorageBarrier("draft-read-error");
        report.checked = true;
        this.ready = true;
        this.emit();
        return report;
      }
      const draft = draftRead.status === "ok" ? draftRead.value : null;
      const outbox = outboxRead.status === "ok" ? outboxRead.value : [];
      if (generation !== this.generation || this.disposed) return report;
      const mine = (outbox ?? []).filter((item) => String(item?.task_id) === String(context.taskId));
      report.pendingOutbox = mine.length;
      const terminal = mine.filter((item) => isTerminalOutboxRoute(item?.route));
      if (terminal.length > 0) {
        // P1 cannot continue past terminal requests: retain everything and
        // route back to the legacy page. Never replay, never overwrite.
        this.terminalOutbox = terminal;
        report.terminalOutbox = terminal;
        this.routeToLegacy = true;
        this.freeze("terminal-outbox");
        report.checked = true;
        this.ready = true;
        this.emit();
        return report;
      }
      const checkedDraft = validateStoredDraft(draft);
      if (draft && !checkedDraft.ok) {
        // Corrupt record: keep it as a write barrier so the next input does
        // not overwrite the same key before the user exports/discards (V03).
        report.checked = true;
        this.ready = true;
        this.setStorageBarrier("corrupt-draft");
        return report;
      }
      // Base revision / unsynced state remembered for the post-replay
      // conflict verdict below (R03): only a gap that no own replayed
      // operation explains may freeze as an external conflict.
      let postReplayBase: number | null = null;
      let postReplayUnsynced = false;
      if (checkedDraft.ok && checkedDraft.draft) {
        report.storedDraft = true;
        this.lastStoredDraft = checkedDraft.draft as unknown as Record<string, unknown>;
        const stored = checkedDraft.draft;
        if (
          String(stored.lease_token) !== String(context.leaseToken) ||
          String(stored.version_id) !== String(context.versionId)
        ) {
          this.conflict = {
            kind: "lease-mismatch",
            localRevision: typeof stored.server_revision === "number" ? stored.server_revision : null,
            serverRevision: context.revision,
          };
          // The stored record is an earlier lease/version draft; the editor
          // holds the fresh server snapshot. Guard it from overwrite (N01).
          this.preservedDraftConflict = true;
          report.conflict = this.conflict;
          this.freeze("assignment-invalid");
        } else {
          // Tentatively restore first; the revision-gap verdict comes
          // AFTER the outbox replay below, so our own accepted-but-
          // unacknowledged operations can explain the gap before any
          // external-conflict freeze (R03).
          const storedHasPending =
            stored.status === "dirty" ||
            (Array.isArray(stored.dirty_segment_ids) && stored.dirty_segment_ids.length > 0) ||
            stored.review_dirty === true ||
            mine.length > 0;
          if (
            typeof stored.server_revision === "number" &&
            Number(stored.server_revision) !== Number(context.revision) &&
            !storedHasPending
          ) {
            // Stale clean cache: the server moved on while this device had
            // nothing unsynced. Keep the fresh server state; restoring the
            // old snapshot would rewind the editor and the revision (R09).
          } else {
            this.restoreDraft(stored);
          }
          postReplayBase =
            typeof stored.server_revision === "number" ? stored.server_revision : null;
          postReplayUnsynced =
            stored.status === "dirty" ||
            (Array.isArray(stored.dirty_segment_ids) && stored.dirty_segment_ids.length > 0) ||
            stored.review_dirty === true;
        }
      }
      if (!this.conflict) {
        const replayed = await this.runReplay();
        if (generation !== this.generation || this.disposed) {
          this.queue.resumeAfterReplay([]);
          return report;
        }
        if (replayed === null) {
          // A concurrent replay owned the writer and already applied the
          // results; nothing further to do here.
          return report;
        }
        report.replayed = replayed.replayed;
        if (replayed.readError) {
          // X03: the replay read failed. This is NOT an empty queue; keep the
          // barrier so no newer draft is sent ahead of unknown operations.
          report.checked = true;
          this.ready = true;
          this.queue.resumeAfterReplay([]);
          this.setStorageBarrier("replay-read-error");
          return report;
        }
        if (replayed.corruptItems.length > 0 || replayed.unknownItems.length > 0) {
          // Unknown/corrupt older operations (V04/Y05): block new saves and
          // surface them so the user can export/return/discard before
          // anything skips past. Unknown envelopes are never reinterpreted.
          report.checked = true;
          this.ready = true;
          this.queue.resumeAfterReplay([]);
          this.setStorageBarrier("corrupt-outbox");
          return report;
        }
        if (replayed.terminal.length > 0) {
          this.terminalOutbox = replayed.terminal;
          this.routeToLegacy = true;
          report.conflict = null;
          this.queue.resumeAfterReplay([]);
          this.freeze("terminal-outbox");
          report.checked = true;
          this.ready = true;
          this.emit();
          return report;
        }
        if (replayed.conflicted.length > 0) {
          // Our own immutable operation was rejected: the server genuinely
          // moved on without us. Freeze with the local work preserved.
          this.conflict = {
            kind: "revision-mismatch",
            localRevision: postReplayBase,
            serverRevision: null,
          };
          report.conflict = this.conflict;
          this.freeze("revision-conflict");
        } else if (
          replayed.applied.length === 0 &&
          replayed.failed.length === 0 &&
          postReplayBase !== null &&
          Number(postReplayBase) !== Number(context.revision) &&
          postReplayUnsynced
        ) {
          // No own operation explains the gap and nothing is still
          // retryable: a genuine external change. (Failed replays stay
          // pending with an offline error instead of freezing.)
          this.conflict = {
            kind: "revision-mismatch",
            localRevision: postReplayBase,
            serverRevision: context.revision,
          };
          report.conflict = this.conflict;
          this.freeze("revision-conflict");
        }
        if (replayed.failed.length > 0 && !this.conflict) {
          // Older operation failed to replay (W03): new saves must not jump
          // ahead of it. Block until a replay succeeds (retry/online).
          this.storageBarrier = "replay-failed";
          this.error = this.barrierMessage("replay-failed");
        }
        // Resume the writer only after the conflict verdict: a freeze above
        // must not be bypassed by a queued drain. A successful startup
        // preflight lets the writer continue; a barrier does not.
        this.queue.resumeAfterReplay([]);
        if (!this.conflict && !this.blocked()) this.queue.continueAfterReplay();
      }
      report.checked = true;
      this.ready = true;
      this.needsRefetch = false;
      this.emit();
      if (!this.conflict && this.needsSave()) {
        this.scheduleAutosave();
      }
      return report;
    } catch {
      if (generation !== this.generation || this.disposed) return report;
      this.error = "Local storage is unavailable. Editing continues without a local copy.";
      report.checked = true;
      report.storageHealth = "unavailable";
      this.ready = true;
      this.emit();
      return report;
    }
  }

  private restoreDraft(stored: {
    segments: Array<Record<string, unknown>>;
    dirty_segment_ids?: Array<number | string>;
    scene_review?: SceneReviewValue | null;
    review_dirty?: boolean;
    server_revision?: number;
    pending_time_edits?: unknown;
  }): void {
    const dirty = (stored.dirty_segment_ids ?? []).map(String);
    this.editor.restoreFromDraft(
      stored.segments.map((seg) => ({ ...(seg as object) }) as never),
      dirty,
    );
    if (stored.scene_review !== undefined) {
      this.review = cloneReview(stored.scene_review);
      this.reviewDirty = Boolean(stored.review_dirty);
    }
    if (typeof stored.server_revision === "number") {
      this.revision = stored.server_revision;
    }
    // AA03: restore validated raw time text so a time-only edit survives a
    // reload. Invalid entries are ignored defensively.
    if (Array.isArray(stored.pending_time_edits)) {
      for (const raw of stored.pending_time_edits) {
        if (!raw || typeof raw !== "object") continue;
        const entry = raw as Record<string, unknown>;
        const field = entry.field;
        const segmentId = entry.segment_id;
        if (field !== "start" && field !== "end") continue;
        if (typeof segmentId !== "string" && typeof segmentId !== "number") continue;
        if (typeof entry.raw !== "string") continue;
        if (!this.editor.getSegment(segmentId)) continue;
        this.editor.setPendingTime(segmentId, field, entry.raw);
      }
    }
  }

  // ---- storage barrier ----

  private barrierMessage(kind: string): string {
    switch (kind) {
      case "draft-read-error":
        return "Could not read this task's local draft. Editing is paused so unknown local data is not overwritten. Retry in the classic workspace.";
      case "corrupt-draft":
        return "Local data on this device is damaged but was kept. Editing is paused; export what you can, or discard explicitly before continuing.";
      case "corrupt-outbox":
        return "An older unsent request on this device is unreadable. New saves are paused until you export or discard it in the classic workspace.";
      case "replay-read-error":
        return "This task's unsent requests could not be read. New saves are paused so they do not skip ahead of unknown operations.";
      case "replay-failed":
        return "An older unsent save could not be replayed yet. New saves are paused so they do not skip ahead of it.";
      case "storage-error":
        return "Local storage failed. Edits are kept in memory but are not backed up on this device.";
      default:
        return "Local storage needs attention before saving can continue.";
    }
  }

  private setStorageBarrier(kind: string | null): void {
    this.storageBarrier = kind;
    if (kind !== null) this.error = this.barrierMessage(kind);
    this.emit();
  }

  /**
   * Z03: error text is derived from the active barrier first, so a barrier
   * that persists (e.g. replay-read-error) always has a matching visible
   * message even if another path cleared `this.error`.
   */
  private effectiveError(): string | null {
    if (this.storageBarrier !== null) return this.barrierMessage(this.storageBarrier);
    if (this.unconfirmedIds.size > 0) {
      return "The server saved this task, but the local pending-request record could not be updated. Retrying.";
    }
    return this.error;
  }

  /** True when any barrier currently forbids WRITES (network or local). */
  private blocked(): boolean {
    return this.storageBarrier !== null;
  }

  /**
   * Barriers that mean an unknown/corrupt local record exists and must not
   * be silently added to. Editing itself may continue in memory (so the user
   * can export), but nothing may be written to that key nor sent until the
   * user resolves it. A plain quota/`storage-error` is NOT in this set:
   * memory editing and export must keep working (V02/U04).
   */
  private unknownLocalData(): boolean {
    return (
      this.storageBarrier === "corrupt-draft" ||
      this.storageBarrier === "draft-read-error" ||
      this.storageBarrier === "corrupt-outbox" ||
      this.storageBarrier === "replay-read-error"
    );
  }

  /**
   * Barriers that must stop a NETWORK write: unknown/corrupt local records
   * (must be resolved first) and an older operation that failed to replay
   * (W03: new saves may not skip ahead of it). A pure `storage-error`
   * (quota) does NOT block network saves — the server copy is still useful,
   * but the local-backup failure stays visible (V02).
   */
  private networkBlocked(): boolean {
    return this.unknownLocalData() || this.storageBarrier === "replay-failed";
  }

  // ---- editing ----

  updateSegment(id: number | string, patch: SegmentPatch): void {
    if (!this.context || this.frozenReason || this.recoveryPending || this.disposed) return;
    this.editor.updateSegment(id, patch);
    this.afterEdit();
  }

  updateReview(next: SceneReviewValue): void {
    if (!this.context || this.frozenReason || this.recoveryPending || this.disposed) return;
    // Feature-gated review (W05): when writes are off, reject the change so
    // it can never create an unsaveable dirty state.
    if (!reviewWriteEnabled(this.context.features)) return;
    if (reviewsEqual(this.review, next)) return;
    this.review = cloneReview(next) ?? next;
    this.reviewDirty = true;
    this.error = null;
    this.afterEdit();
  }

  private afterEdit(): void {
    // A transient save error clears on the next edit, but a storage barrier
    // or an outstanding unconfirmed operation must stay visible until
    // resolved (V03/V04/W03/Y01).
    if (this.storageBarrier === null && this.unconfirmedIds.size === 0) this.error = null;
    this.queue.noteInput(this.currentInput());
    void this.persistSoon();
    this.scheduleAutosave();
    this.emit();
  }

  private currentInput(): {
    segments: Array<Record<string, unknown>>;
    review: unknown;
    includeReview: boolean;
    leaseToken: string;
    revision: number;
  } {
    const context = this.context;
    return {
      segments: this.editor.dirtyPayload().map((seg) => ({ ...(seg as unknown as Record<string, unknown>) })),
      review: cloneReview(this.review),
      includeReview: shouldSaveReview(context?.features ?? null, this.reviewDirty, this.review),
      leaseToken: context?.leaseToken ?? "",
      revision: this.revision ?? context?.revision ?? 0,
    };
  }

  needsSave(): boolean {
    if (!this.context || !this.ready || this.frozenReason || this.disposed) return false;
    if (this.editor.hasDirty()) return true;
    return shouldSaveReview(this.context.features, this.reviewDirty, this.review);
  }

  /**
   * Single unsaved-work definition used by the draft status, the badge and
   * the leave guard (AA04): any dirty segment/review, pending time text, or
   * unconfirmed accepted operation means "not clean". Persisted/exported
   * `status` must be derived from exactly this.
   */
  hasDirtyContent(): boolean {
    return (
      this.editor.hasDirty() ||
      this.reviewDirty ||
      this.editor.hasPendingTimes() ||
      this.unconfirmedIds.size > 0
    );
  }

  hasUnsavedWork(): boolean {
    return (
      this.hasDirtyContent() ||
      this.queue.pendingCount() > 0 ||
      this.conflict !== null ||
      this.terminalOutbox.length > 0
    );
  }

  // ---- local persistence (always before any send) ----

  /**
   * Commit any pending time-field text into the domain store (X05). A
   * complete, valid value is parsed+clamped+applied atomically; an invalid
   * or incomplete value stays pending and returns a field handle so the
   * caller can focus it and abort the send. Manual save, Ctrl/Cmd+S and
   * autosave all go through here so intermediate typing never reaches the
   * server.
   */
  commitPendingTimeEdits(): { ok: boolean; invalid: Array<{ id: string; field: "start" | "end" }> } {
    const entries = this.editor.pendingTimeEntries();
    const invalid: Array<{ id: string; field: "start" | "end" }> = [];
    const segments = this.editor.getSnapshot().segments;
    for (const entry of entries) {
      const parsed = parseTimeInput(entry.raw);
      const current = this.editor.getSegment(entry.id);
      if (!current || parsed === null) {
        invalid.push({ id: entry.id, field: entry.field });
        continue;
      }
      const index = segments.findIndex((seg) => String(seg.id) === entry.id);
      const min = index > 0 ? Number(segments[index - 1]?.end ?? 0) : 0;
      const duration = this.context?.duration ?? 0;
      const max =
        index >= 0 && index < segments.length - 1
          ? Number(segments[index + 1]?.start ?? parsed)
          : (Number.isFinite(duration) && duration > 0 ? duration : parsed);
      const next = clampTimeEdit(entry.field, parsed, current, { min, max });
      const end = entry.field === "start" ? Number(current.end) : next;
      const start = entry.field === "start" ? next : Number(current.start);
      this.editor.clearPendingTime(entry.id, entry.field);
      this.updateSegment(entry.id, {
        [entry.field]: next,
        duration: Math.round((end - start) * 1000) / 1000,
      });
    }
    return { ok: invalid.length === 0, invalid };
  }

  /**
   * Persist the current draft to IndexedDB. Returns true on success. A
   * failure raises the storage barrier and marks `localWriteFailed`; it is
   * NEVER cleared by a later server success (V02).
   */
  async persistLocal(): Promise<boolean> {
    if (!this.context || this.disposed) return false;
    // Terminal outbox entries belong to flows P1 does not own: never touch
    // the stored records while they are retained for the legacy page.
    if (this.terminalOutbox.length > 0) return false;
    // Recovery gate (T02/U03): while a conflict recovery is in progress or
    // failed, the in-memory editor may still hold content the user asked to
    // discard. No local snapshot may be written back; navigation falls back
    // to the authoritative (server or still-valid) draft instead.
    if (this.recoveryPending) return false;
    // Corrupt/unreadable local state (V03/W08): never overwrite the same key
    // until the user resolves it.
    if (this.storageBarrier === "corrupt-draft" || this.storageBarrier === "draft-read-error") {
      return false;
    }
    // Preserved-draft conflict (N01): the stored record belongs to an
    // earlier lease/version and is what the user can still export; the
    // in-memory editor only holds the fresh server snapshot. Writing here
    // would overwrite the preserved draft with that snapshot. A conflict
    // raised by the current editor's own save (preservedDraftConflict
    // false) is different and must still persist the newest input first.
    if (this.preservedDraftConflict) return false;
    const context = this.context;
    const snapshot = this.editor.getSnapshot();
    try {
      const stored = await this.store.saveWorkingDraft({
        schema_version: 1,
        updated_at: new Date().toISOString(),
        status: this.hasDirtyContent() ? "dirty" : "clean",
        username: context.username,
        task_id: context.taskId,
        version_id: context.versionId,
        lease_token: context.leaseToken,
        server_revision: this.revision ?? context.revision,
        segments: snapshot.segments,
        scene_review: cloneReview(this.review),
        dirty_segment_ids: this.editor.dirtyIds().map((key) => this.editor.getSegment(key)?.id ?? key),
        // Record only the actual unconfirmed review flag. OR-ing the
        // in-flight state here would resurrect a phantom "review unsaved"
        // on reload and cause a spurious review-only save (revision bump
        // with unchanged content). In-flight reviews are covered by the
        // outbox replay + confirmOutbox(clear_dirty) path instead.
        review_dirty: this.reviewDirty,
        mode: context.mode ?? "",
        round_id: context.roundId ?? "",
        // AA03: raw time text is recoverable unsaved work, stored as an
        // optional backward-compatible field (never in numeric segments).
        ...(this.editor.hasPendingTimes()
          ? {
              pending_time_edits: this.editor
                .pendingTimeEntries()
                .map((entry) => ({
                  segment_id: entry.id,
                  field: entry.field,
                  raw: entry.raw,
                })),
            }
          : {}),
      });
      this.lastStoredDraft = stored as unknown as Record<string, unknown>;
      // A genuine successful local write clears the local-write failure.
      this.localWriteFailed = false;
      if (this.storageBarrier === "storage-error" && this.storeHealth() === "ok") {
        this.storageBarrier = null;
      }
      if (
        this.error &&
        this.storeHealth() === "ok" &&
        !this.localWriteFailed &&
        this.unconfirmedIds.size === 0 &&
        this.storageBarrier === null
      ) {
        // Z03: never clear the error while a barrier (replay-read-error,
        // corrupt, etc.) still explains it.
        this.error = null;
      }
      this.emit();
      return true;
    } catch {
      this.localWriteFailed = true;
      this.storageBarrier = "storage-error";
      this.error = this.barrierMessage("storage-error");
      this.emit();
      return false;
    }
  }

  private persistSoon(): Promise<void> {
    if (this.persistTimer !== null) this.clearTimeoutImpl(this.persistTimer);
    return new Promise((resolve) => {
      this.persistTimer = this.setTimeoutImpl(() => {
        this.persistTimer = null;
        void this.persistLocal().then(() => resolve());
      }, this.persistDelay);
    });
  }

  private scheduleAutosave(): void {
    if (!this.needsSave()) return;
    if (this.autosaveTimer !== null) this.clearTimeoutImpl(this.autosaveTimer);
    this.autosaveTimer = this.setTimeoutImpl(() => {
      this.autosaveTimer = null;
      void this.flush().catch(() => undefined);
    }, this.autosaveDelay);
  }

  // ---- queue transport (persist -> stage -> send) ----

  private async stageOutbox(op: QueuedOperation): Promise<void> {
    if (!this.context) throw new ApiError("No assignment", 400, {});
    await this.persistLocal();
    await this.store.putOutbox({
      operation_id: op.operationId,
      username: this.context.username,
      task_id: this.context.taskId,
      route: SAVE_ROUTE,
      method: SAVE_METHOD,
      body: op.body,
      created_at: new Date(op.enqueuedAt).toISOString(),
      attempts: op.attempts,
    });
  }

  private async sendSave(body: SaveRequestBody, options: { silent?: boolean } = {}): Promise<SaveResult> {
    try {
      return await this.sender(body);
    } catch (error) {
      if (!options.silent) this.handleSendError(error);
      throw error;
    }
  }

  private handleSendError(error: unknown): void {
    const status = Number((error as { status?: unknown })?.status ?? 0);
    const code = (error as { code?: unknown })?.code as string | undefined;
    const kind = classifyFailure(Number.isFinite(status) ? status : 0, code);
    if (kind === "auth") {
      this.error = genericErrorMessage(status, code);
      void this.persistLocal().then(() => {
        this.freeze(code === "account_deactivated" ? "account-deactivated" : "unauthorized");
        void this.session?.handleUnauthorized({ code: code ?? "not_authenticated" }, { hasDirty: true });
      });
      return;
    }
    if (kind === "conflict") {
      this.conflict = {
        kind: "revision-mismatch",
        localRevision: this.revision,
        serverRevision: Number((error as { body?: { current_revision?: unknown } })?.body?.current_revision ?? NaN) || null,
      };
      this.error = genericErrorMessage(status, code);
      void this.persistLocal().then(() => this.freeze("revision-conflict"));
      return;
    }
    if (kind === "terminal") {
      this.error = genericErrorMessage(status, code);
      this.freeze("assignment-invalid");
      return;
    }
    this.error = genericErrorMessage(status, code);
    this.emit();
  }

  /**
   * Queue confirm callback. Returns whether the local confirmation committed
   * (Z01): `false` keeps the operation in the writer's accepted-but-
   * unconfirmed phase so later operations cannot pass it.
   */
  private async confirmOperation(op: QueuedOperation, result: SaveResult): Promise<boolean> {
    // Actor/task/version/lease changed while the request was in flight: the
    // stale confirmation must not touch the new context. Treat it as
    // "handled" so the writer does not park on an obsolete operation.
    if (!this.context || op.body.lease_token !== this.context.leaseToken) return true;
    // Z02: never move the revision backwards. A late idempotent reply for an
    // older operation must not override a higher confirmed revision.
    if (this.revision === null || result.revision > this.revision) {
      this.revision = result.revision;
      if (this.context) this.context.revision = result.revision;
    }
    this.editor.clearDirtyWhereEqual(
      op.sentSegments.map((seg) => ({ ...(seg as object) }) as never),
    );
    if (op.sentReview !== undefined && reviewsEqual(this.review, op.sentReview as SceneReviewValue | null)) {
      this.reviewDirty = false;
    }
    let committed = true;
    try {
      await this.store.confirmOutbox(op.operationId, {
        server_revision: result.revision,
        clear_dirty: true,
      });
      // X02/Y01: only THIS operation's success removes ITS OWN unconfirmed id.
      this.unconfirmedIds.delete(op.operationId);
    } catch {
      // The server accepted the save, but the local outbox confirmation
      // failed. The operation is accepted-but-unconfirmed; track it BY ID so
      // another operation's success cannot clear it (X02/Y01).
      committed = false;
      this.unconfirmedIds.add(op.operationId);
      this.error = "The server saved this task, but the local pending-request record could not be updated. Retrying.";
    }
    // Only a successful local write clears the "not saved on this device"
    // state (V02). A server 200 must never mask a still-failing backup.
    const persisted = await this.persistLocal();
    if (persisted) {
      this.localWriteFailed = false;
      if (this.storageBarrier === "storage-error" && this.unconfirmedIds.size === 0) {
        this.storageBarrier = null;
      }
    }
    // The error clears only when the local backup succeeded and there are no
    // outstanding unconfirmed operations.
    if (persisted && this.unconfirmedIds.size === 0) this.error = null;
    this.emit();
    return committed;
  }

  private async bumpAttempt(op: QueuedOperation): Promise<void> {
    try {
      await this.store.bumpOutboxAttempt(op.operationId);
    } catch {
      // Attempt bookkeeping must not break the retry loop.
    }
  }

  private onQueueChanged(): void {
    const snapshot = this.queue.getSnapshot();
    if (snapshot.frozenReason && !this.frozenReason) {
      const reason = snapshot.frozenReason;
      if (reason.startsWith("no-retry:401") || reason.startsWith("no-retry:0")) {
        // Auth/offline paths already updated status via handleSendError or
        // stay in retry; only adopt unexpected queue freezes here.
      }
      if (reason.startsWith("no-retry:409")) {
        this.freeze("revision-conflict");
        return;
      }
      if (
        reason.startsWith("no-retry:400") ||
        reason.startsWith("no-retry:403") ||
        reason.startsWith("no-retry:404") ||
        reason.startsWith("no-retry:422")
      ) {
        this.freeze("assignment-invalid");
        return;
      }
    }
    if (
      !this.frozenReason &&
      !this.disposed &&
      !this.networkBlocked() &&
      !this.preflightActive &&
      snapshot.status === "idle" &&
      snapshot.queuedIds.length === 0 &&
      this.needsSave()
    ) {
      // Continue only with input that arrived while a write was in flight
      // (the A-then-B flow). buildNext() returns null when nothing new was
      // noted, so a merely-dirty editor (e.g. right after discard + reset,
      // before the authoritative re-attach lands) never manufactures a
      // phantom operation from stale dirty flags. A waiting input is
      // re-stamped first: its revision/lease were captured at note time and
      // may have moved on when the in-flight write confirmed.
      if (this.queue.hasPendingInput()) {
        this.queue.noteInput(this.currentInput());
      }
      const next = this.queue.buildNext();
      if (next) this.queue.enqueue(next);
    }
    this.emit();
  }

  // ---- manual / auto save entry points ----

  async flush(options: { manual?: boolean } = {}): Promise<void> {
    if (!this.context || !this.ready || this.frozenReason || this.disposed) return;
    const manual = options.manual === true;
    // Recovery gate (T02): while a conflict recovery is in progress or has
    // failed, no write path (button, Ctrl+S, autosave, online) may send.
    if (this.recoveryPending) return;
    // Z01: if the writer holds an accepted-but-unconfirmed operation, retry
    // its LOCAL confirmation before anything else; only once it commits may a
    // later operation proceed.
    if (this.queue.unconfirmedId !== null) {
      await this.queue.retryUnconfirmed();
      if (this.queue.unconfirmedId !== null) return;
    }
    // X05/Y07/Y08: only a MANUAL save (button / Ctrl+S) resolves in-progress
    // time edits. Automatic saves and hidden flushes must not publish a
    // focused partial value. A manual commit that finds an invalid/incomplete
    // field blocks the whole send (the field keeps its raw text + error).
    if (manual && this.editor.hasPendingTimes()) {
      const committed = this.commitPendingTimeEdits();
      if (!committed.ok) return;
    }
    if (this.autosaveTimer !== null) {
      this.clearTimeoutImpl(this.autosaveTimer);
      this.autosaveTimer = null;
    }
    if (manual) {
      // W03/Y03: reconcile the persisted outbox BEFORE allowing any new send,
      // so an older on-disk operation (possibly written by another tab) is
      // confirmed first and the new input uses the confirmed revision. The
      // manual retry budget is reopened only after this preflight.
      const replayedOlder = await this.reconcileBeforeWrite();
      if (this.blocked()) return;
      this.queue.retryManual();
      if (replayedOlder) {
        // The older op just confirmed and advanced the revision. Show the
        // confirmed revision briefly, then send the newer input through the
        // same serial queue (W03: A then B, observable in order).
        this.followUpPending = true;
        this.emit();
        const input = this.currentInput();
        this.setTimeoutImpl(() => {
          this.followUpPending = false;
          if (this.disposed || this.frozenReason || this.recoveryPending || this.blocked()) {
            this.emit();
            return;
          }
          if (this.needsSave() || input.segments.length > 0) {
            this.queue.noteInput(input);
            const next = this.queue.buildNext();
            if (next) this.queue.enqueue(next);
          }
          this.emit();
        }, 400);
        return;
      }
      // If a write is somehow already active/queued, let it finish instead
      // of building a duplicate operation.
      const active = this.queue.getSnapshot();
      if (active.status !== "idle" || active.queuedIds.length > 0) {
        await this.queue.drain();
        return;
      }
    } else if (this.networkBlocked()) {
      // Automatic paths must not skip past unknown/failed older local state.
      return;
    }
    if (!this.needsSave() && this.queue.pendingCount() === 0) return;
    this.queue.noteInput(this.currentInput());
    const snapshot = this.queue.getSnapshot();
    if (snapshot.status === "idle" && snapshot.queuedIds.length === 0) {
      const next = this.queue.buildNext();
      if (next) this.queue.enqueue(next);
    }
    await this.queue.drain();
  }

  /**
   * Re-run the persisted outbox before any new write (W03). Returns true when
   * an older operation was replayed in this call (the caller may have just
   * advanced the revision and should let the new input follow asynchronously).
   */
  private async reconcileBeforeWrite(): Promise<boolean> {
    if (!this.context) return false;
    // If the serial queue already holds an operation (in flight, queued, or
    // waiting on its bounded backoff), it OWNS the writer. Do not start a
    // parallel replay: the queue's own retry will process it in order (R01).
    const q = this.queue.getSnapshot();
    if (q.status === "sending" || q.status === "waiting-retry" || q.queuedIds.length > 0) {
      return false;
    }
    // AB02: the whole preflight (local confirm + outbox replay) blocks the
    // auto-enqueue path so newer input cannot overtake a persisted X.
    this.preflightActive = true;
    try {
      if (this.queue.unconfirmedId !== null) {
        await this.queue.retryUnconfirmed();
        if (this.queue.unconfirmedId !== null) return false;
      }
      const replayed = await this.runReplay();
      if (replayed === null) return false; // a concurrent replay owns the writer
      if (replayed.readError) {
        // Y04: a read failure on any save path is a shared blocking result.
        this.queue.releaseReplay([]);
        this.setStorageBarrier("replay-read-error");
        return false;
      }
      if (replayed.unknownItems.length > 0) {
        this.queue.releaseReplay([]);
        this.setStorageBarrier("corrupt-outbox");
        return false;
      }
      if (replayed.corruptItems.length > 0) {
        this.queue.releaseReplay([]);
        this.setStorageBarrier("corrupt-outbox");
        return false;
      }
      if (replayed.failed.length > 0) {
        this.queue.releaseReplay([]);
        this.storageBarrier = "replay-failed";
        this.error = this.barrierMessage("replay-failed");
        this.emit();
        return false;
      }
      // Older work cleared: drop any queued op the replay already covered and
      // allow the new input through.
      this.queue.releaseReplay(replayed.applied.map((entry) => entry.item.operation_id));
      if (this.storageBarrier === "replay-failed") this.storageBarrier = null;
      return replayed.applied.length > 0;
    } finally {
      this.preflightActive = false;
    }
  }

  freeze(reason: FreezeReason): void {
    if (this.frozenReason) return;
    this.frozenReason = reason;
    if (this.autosaveTimer !== null) {
      this.clearTimeoutImpl(this.autosaveTimer);
      this.autosaveTimer = null;
    }
    if (this.persistTimer !== null) {
      this.clearTimeoutImpl(this.persistTimer);
      this.persistTimer = null;
    }
    this.queue.freeze(reason);
    this.emit();
  }

  private handleSessionFrozen(code: FreezeCode): void {
    if (code === "account_deactivated") this.freeze("account-deactivated");
    else if (!this.frozenReason) this.freeze("unauthorized");
    this.error = genericErrorMessage(code === "account_deactivated" ? 403 : 401, code);
    this.emit();
  }

  /**
   * Apply replay confirmations to in-memory state: advance the revision and
   * clear only the dirty content covered by each sent snapshot. Shared by
   * attachAssignment (startup) and handleOnline so both recovery paths use
   * the identical serial reconciliation (N02).
   */
  private applyReplayApplied(applied: ReplayOutcome["applied"]): void {
    for (const entry of applied) {
      // Z02: revision is monotonic. A late idempotent reply for an older
      // operation carries an older revision; it must NOT rewind a revision a
      // later operation already advanced.
      if (this.revision === null || entry.revision > this.revision) {
        this.revision = entry.revision;
        if (this.context) this.context.revision = entry.revision;
      }
      this.editor.clearDirtyWhereEqual(
        (Array.isArray(entry.item.body.segments) ? entry.item.body.segments : []).map(
          (seg) => ({ ...(seg as object) }) as never,
        ),
      );
      const sentReview = (entry.item.body as { scene_review?: unknown }).scene_review;
      if (
        sentReview !== undefined &&
        reviewsEqual(this.review, sentReview as SceneReviewValue | null)
      ) {
        this.reviewDirty = false;
      }
    }
  }

  async handleOnline(): Promise<void> {
    if (!this.context || this.frozenReason || this.recoveryPending || this.disposed) return;
    await this.persistLocal();
    this.preflightActive = true;
    try {
      await this.runRecoveryPreflight();
    } finally {
      this.preflightActive = false;
    }
    if (this.disposed) return;
    // Only after the preflight (confirm unconfirmed A → replay persisted X)
    // may newer input be built/sent. This guarantees A, X, B order (AB02).
    if (this.needsSave()) {
      await this.flush();
    } else {
      this.queue.continueAfterReplay();
      this.emit();
    }
  }

  /**
   * One shared recovery preflight used by startup/online/manual (AB02):
   * 1. confirm the accepted-but-unconfirmed A locally (no network),
   * 2. replay the persisted outbox X in order,
   * 3. surface any barrier/freeze.
   * Returns true when the writer may proceed to build newer input.
   */
  private async runRecoveryPreflight(): Promise<boolean> {
    const replayed = await this.replayOrConfirm();
    return replayed;
  }

  /**
   * Runs runReplay and applies the resulting verdict. Returns true when the
   * preflight succeeded (no barrier/freeze), false when the caller must stop.
   */
  private async replayOrConfirm(): Promise<boolean> {
    // AA01: if the writer already holds a server-accepted operation whose
    // LOCAL confirmation failed, retry that confirmation first. The page
    // knows A's revision, so this must not require a network replay.
    if (this.queue.unconfirmedId !== null) {
      await this.queue.retryUnconfirmed();
      if (this.queue.unconfirmedId !== null) return false;
    }
    // Serial recovery (single writer, N02/U02): every online event funnels
    // through runReplay, whose mutex means concurrent online handlers share
    // ONE outbox replay instead of racing two.
    const replayed = await this.runReplay();
    if (this.disposed) return false;
    if (replayed === null) {
      // A concurrent replay owned the writer and already applied results.
      return !this.blocked();
    }
    // Y04: a read failure is a shared blocking result. Keep the barrier (set
    // BEFORE resuming the queue) and do not flush newer input.
    if (replayed.readError) {
      this.setStorageBarrier("replay-read-error");
      this.queue.resumeAfterReplay([]);
      return false;
    }
    if (replayed.unknownItems.length > 0) {
      this.setStorageBarrier("corrupt-outbox");
      this.queue.resumeAfterReplay([]);
      return false;
    }
    if (replayed.terminal.length > 0) {
      this.terminalOutbox = replayed.terminal;
      this.routeToLegacy = true;
      this.queue.resumeAfterReplay([]);
      this.freeze("terminal-outbox");
      return false;
    }
    if (replayed.conflicted.length > 0) {
      this.conflict = {
        kind: "revision-mismatch",
        localRevision: this.revision,
        serverRevision: null,
      };
      this.error = genericErrorMessage(409);
      this.queue.resumeAfterReplay([]);
      this.freeze("revision-conflict");
      return false;
    }
    this.queue.resumeAfterReplay(replayed.applied.map((entry) => entry.item.operation_id));
    if (replayed.corruptItems.length > 0) {
      this.setStorageBarrier("corrupt-outbox");
      return false;
    }
    if (replayed.failed.length > 0) {
      // Keep the barrier: a failed replay means newer saves must wait (W03).
      this.storageBarrier = "replay-failed";
      this.error = this.barrierMessage("replay-failed");
      this.emit();
      return false;
    }
    if (this.storageBarrier === "replay-failed" || this.storageBarrier === "replay-read-error") {
      this.storageBarrier = null;
    }
    return true;
  }

  // ---- conflict / terminal flows (retain, export, discard) ----

  /**
   * Export the newest recoverable draft (U04). The current editor snapshot
   * is authoritative for this task and must be included even when local
   * storage is unwritable (quota) and `lastStoredDraft` is an older
   * snapshot. When a preserved earlier-lease draft exists, it is included
   * under a separate field so neither is silently lost (N01).
   *
   * All blind-marking metadata (`mode`, `round_id`) is stripped recursively
   * from every nested object/value before serialization (V01); the legacy
   * `exportText` only handled the top level.
   */
  exportLocalText(): string {
    const snapshot = this.editor.getSnapshot();
    const current = sanitizeForExport({
      schema_version: 1,
      updated_at: new Date().toISOString(),
      status: this.hasDirtyContent() ? "dirty" : "clean",
      username: this.context?.username ?? "",
      task_id: this.context?.taskId ?? "",
      version_id: this.context?.versionId ?? "",
      lease_token: this.context?.leaseToken ?? "",
      server_revision: this.revision ?? this.context?.revision ?? 0,
      segments: snapshot.segments,
      scene_review: cloneReview(this.review),
      dirty_segment_ids: this.editor.dirtyIds(),
      review_dirty: this.reviewDirty,
      mode: this.context?.mode ?? "",
      round_id: this.context?.roundId ?? "",
    });
    const out: Record<string, unknown> = {
      ...(current as Record<string, unknown>),
    };
    // Z06: include any uncommitted raw time text so a recovery export can
    // actually recover what the user sees in the fields.
    const pendingTimes = this.editor.pendingTimeEntries();
    if (pendingTimes.length > 0) {
      out["pending_time_edits"] = pendingTimes.map((entry) => {
        const seg = this.editor.getSegment(entry.id);
        const valid = parseTimeInput(entry.raw) !== null;
        return {
          segment_id: entry.id,
          field: entry.field,
          raw: entry.raw,
          valid,
          ...(seg ? { current_start: seg.start, current_end: seg.end } : {}),
        };
      });
    }
    if (this.preservedDraftConflict && this.lastStoredDraft) {
      // Never overwrite or hide the earlier-lease draft the user has not
      // yet discarded; expose both, sanitized.
      out["preserved_earlier_lease_draft"] = sanitizeForExport(this.lastStoredDraft);
    }
    return JSON.stringify(out, null, 2);
  }

  async discardLocalConflict(): Promise<void> {
    if (!this.context) return;
    await this.store.deleteTaskData(this.context.username, this.context.taskId);
    this.conflict = null;
    this.lastStoredDraft = null;
    this.needsRefetch = true;
    // Gate every write path until the authoritative assignment is fetched
    // and re-attached (T02): the UI disabled state alone must not be the
    // only protection against Ctrl+S / autosave / online.
    this.recoveryPending = true;
    this.frozenReason = null;
    this.error = null;
    // The queue froze on the 409 (no-retry:409) independently of the
    // controller: without reset() no later save would ever send again.
    this.queue.reset();
    this.emit();
  }

  /** Full navigation back to the legacy workspace; caller persists first. */
  async backToLegacy(): Promise<string> {
    await this.persistLocal();
    return "/";
  }

  // ---- snapshot / lifecycle ----

  storeHealth(): string {
    return (this.store as LegacyOfflineAdapter).storageHealth ?? "ok";
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  getSnapshot(): ControllerSnapshot {
    const queue = this.queue.getSnapshot();
    let status: SaveStatus = "idle";
    if (this.terminalOutbox.length > 0) status = "terminal-unsupported";
    else if (this.frozenReason) status = "frozen";
    else if (this.conflict) status = "conflict";
    else if (
      this.storageBarrier !== null ||
      this.localWriteFailed ||
      this.unconfirmedIds.size > 0 ||
      this.storeHealth() !== "ok"
    ) {
      status = "storage-error";
    } else if (queue.status === "sending") status = "syncing";
    else if (queue.status === "waiting-retry") {
      status = this.hasUnsavedWork() ? "offline" : "syncing";
    } else if (this.hasUnsavedWork()) status = "local";
    else status = "synced";
    return {
      status,
      ready: this.ready,
      revision: this.revision,
      dirtyCount: this.editor.dirtyIds().length,
      reviewDirty: this.reviewDirty,
      review: cloneReview(this.review),
      pendingCount: this.queue.pendingCount(),
      frozenReason: this.frozenReason,
      conflict: this.conflict ? { ...this.conflict } : null,
      terminalOutbox: [...this.terminalOutbox],
      routeToLegacy: this.routeToLegacy,
      needsRefetch: this.needsRefetch,
      error: this.effectiveError(),
      storageHealth: this.storeHealth(),
      hasAssignment: this.context !== null,
      recoveryPending: this.recoveryPending,
      generation: this.editor.currentGeneration,
      storageBarrier: this.storageBarrier,
      storageBarrierMessage: this.storageBarrier === null ? null : this.error,
      localWriteFailed: this.localWriteFailed,
      unconfirmedOperationIds: Array.from(this.unconfirmedIds),
      pendingTimeCount: this.editor.pendingTimeEntries().length,
      features: this.context?.features ?? {},
      followUpPending: this.followUpPending,
    };
  }

  queueSnapshot(): { status: string; inFlightId: string | null; queuedIds: string[] } {
    return this.queue.getSnapshot();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.persistTimer !== null) this.clearTimeoutImpl(this.persistTimer);
    if (this.autosaveTimer !== null) this.clearTimeoutImpl(this.autosaveTimer);
    this.persistTimer = null;
    this.autosaveTimer = null;
    this.queue.dispose();
    this.session?.dispose();
    this.session = null;
    this.listeners.clear();
  }

  private emit(): void {
    if (this.disposed) return;
    const snapshot = this.getSnapshot();
    this.listeners.forEach((listener) => {
      try {
        listener(snapshot);
      } catch {
        // Listener errors must never corrupt controller state.
      }
    });
  }
}
