/**
 * Workspace preview entry: real assignment editing over the existing API.
 * Served at `/frontend-preview/workspace` (Flask gates the annotator
 * session with the legacy reason redirects).
 *
 * - Loads `/api/current-user`, `/api/assignment`, `/api/scenes`; never
 *   claims implicitly. Without an assignment it shows an empty state with
 *   a full navigation back to the classic workspace (claim there first).
 * - Editing runs through the separated lifecycles: `EditorStore` rows,
 *   one shared `MediaController` audio instance, and the
 *   `PersistenceController` serial queue (local-first, immutable replay,
 *   freeze on 401/deactivated/409). Manual save, autosave and Ctrl/Cmd+S
 *   all flush the same queue. No Complete/Skip/Abandon here (P4).
 * - Layout order: app bar -> task title/save state -> transport -> compact
 *   waveform (~120px) -> segment editing -> collapsed scene verification.
 *   No leaderboard column; the transcript keeps the primary width (U06),
 *   and 390px stacks metadata above full-width text (U03).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Input, Popconfirm } from "antd";
import { apiGet, apiSend, isAbortError } from "../../../api/client";
import { AnnotatorShell } from "../../../components/AnnotatorShell";
import { RoleTheme } from "../../../theme/RoleTheme";
import { SessionService, type SessionConfig } from "../../auth/sessionService";
import { SegmentList } from "../editor/SegmentList";
import {
  metadataUiEnabled,
  reviewWriteEnabled,
  type FeatureFlags,
  type SceneReviewValue,
  type SceneTaxonomyEntry,
} from "../editor/sceneReview";
import { SceneReviewForm } from "../editor/SceneReviewForm";
import {
  loadTranscriptFontSize,
  saveTranscriptFontSize,
  TRANSCRIPT_FONT_MAX,
  TRANSCRIPT_FONT_MIN,
  TRANSCRIPT_FONT_STEP,
  type SegmentPatch,
} from "../editor/editorStore";
import { AudioTransport } from "../media/AudioTransport";
import { MediaController } from "../media/mediaController";
import { WaveformPanel } from "../media/WaveformPanel";
import {
  PersistenceController,
  type AssignmentContext,
  type ControllerSnapshot,
} from "../persistence/persistenceController";
import { SaveStatusBadge } from "../persistence/SaveStatus";
import type { SaveResult } from "../persistence/types";
import styles from "./WorkspacePreview.module.css";

interface AssignmentPayload {
  assigned?: boolean;
  task_id?: string;
  version_id?: string;
  lease_token?: string;
  revision?: number;
  filename?: string;
  mode?: string;
  segments?: Array<Record<string, unknown>>;
  waveform_b64?: string | null;
  duration?: number;
  metadata?: {
    scene_review?: SceneReviewValue | null;
    features?: FeatureFlags;
  } | null;
  cross_check?: { round_id?: string } | null;
}

interface ScenesPayload {
  review_taxonomy?: SceneTaxonomyEntry[];
  features?: FeatureFlags;
}

type LoadState =
  | { kind: "loading" }
  | { kind: "empty"; username: string; detail: string }
  | { kind: "failed"; message: string }
  | {
      kind: "ready";
      controller: PersistenceController;
      media: MediaController;
      username: string;
      filename: string;
      taskId: string;
      duration: number;
      waveformB64: string | null;
      taxonomy: SceneTaxonomyEntry[];
    };

function toSegments(raw: Array<Record<string, unknown>> | undefined): Array<Record<string, unknown>> {
  return (raw ?? []).map((seg, index) => ({
    id:
      typeof seg["id"] === "string" || typeof seg["id"] === "number"
        ? (seg["id"] as string | number)
        : index + 1,
    start: Number(seg["start"] ?? 0),
    end: Number(seg["end"] ?? 0),
    duration: Number(seg["duration"] ?? 0),
    text: typeof seg["text"] === "string" ? (seg["text"] as string) : "",
    asr_text: typeof seg["asr_text"] === "string" ? (seg["asr_text"] as string) : "",
    exclude_from_training: seg["exclude_from_training"] === true,
  }));
}

interface FreshAssignment {
  context: AssignmentContext;
  taxonomy: SceneTaxonomyEntry[];
  filename: string;
  taskId: string;
  duration: number;
  waveformB64: string | null;
}

/** Fetch the authoritative assignment + taxonomy; null when unassigned. */
async function fetchFreshAssignment(
  username: string,
  signal?: AbortSignal,
): Promise<FreshAssignment | null> {
  // Bounded retry for a transient assignment read (W07): 4xx never retries;
  // 5xx/network retried a few times before surfacing. Never hides a real
  // unauthenticated/forbidden status.
  let assignment: AssignmentPayload | null = null;
  let lastError: unknown = null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      assignment = await apiGet<AssignmentPayload>("/api/assignment", { signal });
      lastError = null;
      break;
    } catch (error) {
      lastError = error;
      if (isAbortError(error)) throw error;
      const status = (error as { status?: number }).status ?? 0;
      if (status >= 400 && status < 500) throw error;
      // Short bounded backoff so a real failure still surfaces promptly.
      await new Promise((resolve) => setTimeout(resolve, 40 * (attempt + 1)));
    }
  }
  if (lastError) throw lastError;
  if (!assignment) return null;
  if (!assignment.assigned || !assignment.task_id) return null;
  const scenes = await apiGet<ScenesPayload>("/api/scenes", { signal }).catch(
    () => ({}) as ScenesPayload,
  );
  const taskId = String(assignment.task_id);
  const segments = toSegments(assignment.segments);
  return {
    context: {
      username,
      taskId,
      versionId: String(assignment.version_id ?? ""),
      leaseToken: String(assignment.lease_token ?? ""),
      revision: Number(assignment.revision ?? 0),
      segments,
      sceneReview: assignment.metadata?.scene_review ?? null,
      features: assignment.metadata?.features ?? scenes.features ?? {},
      duration: Number(assignment.duration ?? 0),
      ...(typeof assignment.mode === "string" ? { mode: assignment.mode } : {}),
      ...(typeof assignment.cross_check?.round_id === "string"
        ? { roundId: assignment.cross_check.round_id }
        : {}),
    },
    taxonomy: scenes.review_taxonomy ?? [],
    filename: String(assignment.filename ?? taskId),
    taskId,
    duration: Number(assignment.duration ?? 0),
    waveformB64:
      typeof assignment.waveform_b64 === "string" ? assignment.waveform_b64 : null,
  };
}

function WorkspaceBody({
  state,
  onAssignmentReplaced,
}: {
  state: Extract<LoadState, { kind: "ready" }>;
  onAssignmentReplaced: () => void;
}): React.JSX.Element {
  const { controller, media } = state;
  const [snapshot, setSnapshot] = useState<ControllerSnapshot>(() => controller.getSnapshot());
  const [exportText, setExportText] = useState<string | null>(null);
  const [taxonomy, setTaxonomy] = useState<SceneTaxonomyEntry[]>(() => state.taxonomy);
  const boundsKeyRef = useRef<string | null>(null);

  // Stable identity so memoized transcript rows skip re-render when an
  // unrelated row or the save status changes; only the edited row updates.
  const handleUpdateSegment = useCallback(
    (id: number | string, patch: SegmentPatch) => controller.updateSegment(id, patch),
    [controller],
  );
  const handleUpdateReview = useCallback(
    (next: SceneReviewValue) => controller.updateReview(next),
    [controller],
  );

  /**
   * Shared copy command (X08): every panel that offers "Copy" runs this so
   * the blind-metadata-filtered export actually reaches the clipboard, with
   * the read-only textarea as a selectable fallback. One implementation, no
   * duplicated callbacks per panel.
   */
  const copyLocalDraft = useCallback(async () => {
    const text = controller.exportLocalText();
    setExportText(text);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard unavailable: the textarea below is the fallback.
    }
  }, [controller]);

  /**
   * Full conflict recovery (R04/N04): drop the local conflict data, refetch
   * the authoritative assignment, and force a clean re-attach (editor,
   * revision, queue). Recovery runs as its own state machine: editing and
   * saving stay disabled until it succeeds, and a refetch failure keeps an
   * always-visible error with a retry entry (independent of the conflict
   * panel, which discard clears).
   */
  const [recovering, setRecovering] = useState(false);
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  // Transcript font size preference (W06): read the legacy localStorage key
  // on first render, expose +/- controls, and write the clamped value back.
  const [fontSize, setFontSize] = useState<number>(() => loadTranscriptFontSize());
  // Heartbeat reachability (W09): null unknown, false after a failed beat.
  const [online, setOnline] = useState<boolean | null>(
    () => controller.sessionService?.isOnline ?? null,
  );

  const recoverFromConflict = useCallback(async () => {
    setRecoveryError(null);
    setRecovering(true);
    try {
      // discardLocalConflict is idempotent enough to call again on retry:
      // it deletes the same records and resets the already-reset queue.
      await controller.discardLocalConflict();
      const fresh = await fetchFreshAssignment(state.username);
      if (!fresh) {
        setRecoveryError(
          "The assignment is gone. Continue in the classic workspace.",
        );
        return;
      }
      if (fresh.taskId !== state.taskId) {
        // Recovery landed on a DIFFERENT task (U01). The page's title,
        // audio element, waveform, duration and selection belong to the old
        // task; re-attaching only the controller would mix two tasks. Do a
        // full entry reload so every part of the snapshot is same-sourced.
        onAssignmentReplaced();
        return;
      }
      await controller.attachAssignment(fresh.context, { force: true });
      setTaxonomy(fresh.taxonomy);
      setExportText(null);
    } catch (error) {
      setRecoveryError(
        error instanceof Error ? error.message : "Could not reload the assignment.",
      );
    } finally {
      setRecovering(false);
    }
  }, [controller, onAssignmentReplaced, state.username, state.taskId]);

  useEffect(() => controller.subscribe(setSnapshot), [controller]);

  /**
   * Media lifecycle (R08): attach runs here — never in load() — so the
   * page-rendered <audio> element wins (child bind effects run before this
   * parent effect; no hidden fallback instance). Segment bounds stay synced
   * with edited times; text-only edits skip the media commit entirely so
   * typing never pays for canvas/cursor work.
   */
  useEffect(() => {
    const boundsKey = (segments: Array<{ id: unknown; start: unknown; end: unknown }>): string =>
      segments
        .map((seg) => `${String(seg.id)}:${Number(seg.start ?? 0)}-${Number(seg.end ?? 0)}`)
        .join("|");
    const syncBounds = (): void => {
      const segments = controller.editor.getSnapshot().segments;
      const key = boundsKey(segments);
      if (key === boundsKeyRef.current) return;
      boundsKeyRef.current = key;
      media.updateSegments(
        segments.map((seg) => ({
          id:
            typeof seg.id === "string" || typeof seg.id === "number"
              ? seg.id
              : `${segments.indexOf(seg)}`,
          start: Number(seg.start ?? 0),
          end: Number(seg.end ?? 0),
        })),
        state.duration,
      );
    };
    if (media.attachedTaskId !== state.taskId) {
      boundsKeyRef.current = null;
      const segments = controller.editor.getSnapshot().segments;
      boundsKeyRef.current = boundsKey(segments);
      media.attach(
        state.taskId,
        `/api/audio/${state.taskId}`,
        segments.map((seg) => ({
          id:
            typeof seg.id === "string" || typeof seg.id === "number"
              ? seg.id
              : `${segments.indexOf(seg)}`,
          start: Number(seg.start ?? 0),
          end: Number(seg.end ?? 0),
        })),
        state.duration,
      );
    }
    return controller.editor.subscribeBounds(syncBounds);
  }, [controller, media, state.taskId, state.duration]);

  /**
   * Trusted user activity reaches the session heartbeat (R07): only real
   * pointer/keyboard/input events (isTrusted) mark activity, so presence
   * never extends idle/absolute deadlines on synthetic events. The online
   * event re-validates the session before replaying saves. All listeners
   * are removed on unmount/remount (single heartbeat timer).
   */
  useEffect(() => {
    const session = controller.sessionService;
    if (!session) return;
    // Surface heartbeat reachability (W09): a failed beat shows a network
    // banner; the next success clears it.
    const unsubscribeNetwork = session.subscribeNetwork(setOnline);
    const onActivity = (event: Event): void => {
      // Preserve the target for click/audio intent detection; the service
      // itself discards untrusted and irrelevant events.
      const target = event.target as unknown as {
        closest?: (selectors: string) => unknown;
      } | null;
      session.noteEvent({ type: event.type, isTrusted: event.isTrusted, target });
    };
    const onOnline = (): void => {
      void session.handleOnline();
    };
    window.addEventListener("pointerdown", onActivity);
    window.addEventListener("keydown", onActivity);
    window.addEventListener("input", onActivity);
    window.addEventListener("online", onOnline);
    return () => {
      unsubscribeNetwork();
      window.removeEventListener("pointerdown", onActivity);
      window.removeEventListener("keydown", onActivity);
      window.removeEventListener("input", onActivity);
      window.removeEventListener("online", onOnline);
    };
  }, [controller]);

  /**
   * All in-page exits to the legacy workspace persist the latest input
   * first (the autosave debounce must not be allowed to drop keystrokes
   * typed right before navigation). On storage failure the page stays put
   * with the input intact; the error box already shows the recovery path.
   */
  const backToLegacy = useCallback(
    (event: React.MouseEvent<HTMLAnchorElement>) => {
      event.preventDefault();
      void (async () => {
        try {
          await controller.persistLocal();
        } catch {
          // persistLocal reports through snapshot.error already.
        }
        if (controller.storeHealth() !== "ok") return;
        window.location.assign("/");
      })();
    },
    [controller],
  );

  /** Persist the latest draft before navigating to re-login (W02). */
  const persistBeforeRelogin = useCallback(
    (event: React.MouseEvent<HTMLAnchorElement>) => {
      event.preventDefault();
      const href = (event.currentTarget as HTMLAnchorElement).href;
      void controller.persistLocal().finally(() => {
        window.location.assign(href);
      });
    },
    [controller],
  );

  /**
   * Leave guard + hidden flush (W11): while unsaved work or a storage
   * problem exists, warn before unload; when the tab is hidden, flush
   * through the same serial queue. Listeners are cleaned up on unmount.
   */
  useEffect(() => {
    const dirty =
      snapshot.dirtyCount > 0 ||
      snapshot.reviewDirty ||
      snapshot.pendingCount > 0 ||
      // AA03: a time-only edit is unsaved work; persist it on hide too.
      snapshot.pendingTimeCount > 0 ||
      snapshot.unconfirmedOperationIds.length > 0;
    const risky =
      dirty ||
      snapshot.conflict !== null ||
      snapshot.storageBarrier !== null ||
      snapshot.unconfirmedOperationIds.length > 0 ||
      controller.editor.hasPendingTimes();
    const onBeforeUnload = (event: BeforeUnloadEvent): void => {
      if (!risky) return;
      event.preventDefault();
      event.returnValue = "";
    };
    const onVisibility = (): void => {
      if (document.visibilityState !== "hidden" || !dirty) return;
      // X07/Z05: persist the local draft first, then flush through the SAME
      // serial writer (no parallel PATCH). This is an AUTOMATIC intent: it
      // must NOT commit a focused partial time value. Network failure keeps
      // the outbox; reliability comes from the persisted outbox.
      void (async () => {
        await controller.persistLocal();
        await controller.flush();
      })();
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [
    controller,
    snapshot.dirtyCount,
    snapshot.reviewDirty,
    snapshot.pendingCount,
    snapshot.pendingTimeCount,
    snapshot.conflict,
    snapshot.storageBarrier,
    snapshot.unconfirmedOperationIds,
  ]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void controller.flush({ manual: true });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [controller]);

  const frozen = snapshot.frozenReason !== null || snapshot.conflict !== null;
  // Editing is blocked while a conflict is unresolved, while recovery is
  // running or failed (controller-level gate), and after a failed recovery.
  const editingDisabled =
    frozen || snapshot.recoveryPending || recovering || recoveryError !== null || !snapshot.ready;
  const conflictMessage =
    snapshot.conflict?.kind === "revision-mismatch"
      ? `This task changed elsewhere (local revision ${snapshot.conflict.localRevision ?? "?"}). Your edits are kept; copy them out or discard explicitly.`
      : snapshot.conflict?.kind === "lease-mismatch"
        ? "This assignment is no longer yours. Your edits are kept; continue in the classic workspace."
        : null;

  const changeFontSize = (delta: number): void => {
    setFontSize((previous) => {
      const next = saveTranscriptFontSize(previous + delta);
      return next;
    });
  };

  return (
    <div className={styles.page} style={{ ["--transcript-size" as string]: `${fontSize}px` }}>
      <div className={styles.taskbar} data-testid="workspace-taskbar">
        <span className={styles.filename} title={state.filename} data-testid="workspace-filename">
          {state.filename}
        </span>
        <span className={styles.saveRow}>
          <SaveStatusBadge status={snapshot.status} />
          <span role="status" data-testid="workspace-revision">
            rev {snapshot.revision ?? "?"}
            {snapshot.pendingCount === 0 && !snapshot.followUpPending && snapshot.dirtyCount > 0
              ? ` · ${snapshot.dirtyCount} unsaved`
              : ""}
            {snapshot.pendingCount === 0 && !snapshot.followUpPending && snapshot.reviewDirty
              ? " · review unsaved"
              : ""}
            {snapshot.pendingCount === 0 && !snapshot.followUpPending && snapshot.pendingTimeCount > 0
              ? ` · ${snapshot.pendingTimeCount} time unsaved`
              : ""}
          </span>
          <span className={styles.fontControls} role="group" aria-label="Transcript font size">
            <Button
              size="small"
              aria-label="Decrease transcript font size"
              disabled={fontSize <= TRANSCRIPT_FONT_MIN}
              onClick={() => changeFontSize(-TRANSCRIPT_FONT_STEP)}
            >
              A−
            </Button>
            <Button
              size="small"
              aria-label="Increase transcript font size"
              disabled={fontSize >= TRANSCRIPT_FONT_MAX}
              onClick={() => changeFontSize(TRANSCRIPT_FONT_STEP)}
            >
              A+
            </Button>
          </span>
        </span>
      </div>

      {snapshot.routeToLegacy || snapshot.status === "terminal-unsupported" ? (
        <div className={styles.error} role="alert" data-testid="workspace-terminal">
          <p>
            This device holds an older finish/release request that this preview
            cannot continue. Nothing was replayed or deleted.
          </p>
          <p>
            <a href="/" onClick={backToLegacy}>
              Continue in the classic workspace
            </a>
          </p>
        </div>
      ) : null}

      {/* Session frozen (W02): reason, safe draft export and a re-login path,
          matching the legacy page. Only SESSION freezes show here; conflict
          freezes have their own panel below. */}
      {(snapshot.frozenReason === "unauthorized" ||
        snapshot.frozenReason === "account-deactivated") &&
      conflictMessage === null ? (
        <div className={styles.error} role="alert" data-testid="workspace-session-frozen">
          <p className={styles.errorText}>
            {snapshot.frozenReason === "account-deactivated"
              ? "This account was deactivated. This page can no longer save."
              : snapshot.frozenReason === "unauthorized"
                ? "Your session ended. Edits are kept on this device."
                : "Saving is paused for this task."}
          </p>
          <div className={styles.actions}>
            <Button onClick={() => void copyLocalDraft()}>
              Copy local unsaved text
            </Button>
            <Button
              type="primary"
              href="/login.html?reason=not_authenticated"
              onClick={persistBeforeRelogin}
            >
              Sign in again
            </Button>
            <Button type="link" href="/" onClick={backToLegacy}>
              Back to classic workspace
            </Button>
          </div>
          {exportText !== null ? (
            <Input.TextArea
              className={styles.exportBox ?? ""}
              readOnly
              value={exportText}
              aria-label="Local draft export"
              data-testid="workspace-export"
            />
          ) : null}
        </div>
      ) : null}

      {/* Heartbeat network failure (W09): a reachability banner, not a fake
          Saved badge. Clears on the next successful beat. */}
      {online === false && snapshot.frozenReason === null ? (
        <div
          className={styles.error}
          role="status"
          data-testid="workspace-network"
          data-network-state="offline"
        >
          <p className={styles.errorText}>
            Waiting for connection. Edits stay on this device and will sync
            when the network returns.
          </p>
        </div>
      ) : null}

      {conflictMessage ? (
        <div className={styles.error} role="alert" data-testid="workspace-conflict">
          <p className={styles.errorText}>{conflictMessage}</p>
          <div className={styles.actions}>
            <Button onClick={() => void copyLocalDraft()}>
              Copy local draft as text
            </Button>
            <Popconfirm
              title="Discard local changes?"
              description="This removes your local draft and any pending requests on this device. This cannot be undone."
              okText="Discard"
              cancelText="Keep editing"
              okButtonProps={{ danger: true }}
              onConfirm={() => void recoverFromConflict()}
            >
              <Button
                danger
                loading={recovering}
                data-testid="workspace-discard"
              >
                Discard local edits
              </Button>
            </Popconfirm>
            <Button type="link" href="/" onClick={backToLegacy}>
              Back to classic workspace
            </Button>
          </div>
          {exportText !== null ? (
            <Input.TextArea
              className={styles.exportBox ?? ""}
              readOnly
              value={exportText}
              aria-label="Local draft export"
              data-testid="workspace-export"
            />
          ) : null}
        </div>
      ) : null}

      {/* Recovery failure is its own alert (independent of the conflict panel
          that discard cleared), so the error stays visible and retryable. */}
      {recoveryError !== null ? (
        <div className={styles.error} role="alert" data-testid="workspace-reload-error">
          <p className={styles.errorText}>
            Could not reload this task: {recoveryError} Editing stays paused
            until the authoritative task loads.
          </p>
          <div className={styles.actions}>
            <Button
              type="primary"
              onClick={() => void recoverFromConflict()}
              loading={recovering}
              data-testid="workspace-recovery-retry"
            >
              Retry reload
            </Button>
            <Button type="link" href="/" onClick={backToLegacy}>
              Back to classic workspace
            </Button>
          </div>
        </div>
      ) : null}

      {snapshot.error ? (
        <div className={styles.error} role="alert" data-testid="workspace-error">
          <p className={styles.errorText}>{snapshot.error}</p>
        </div>
      ) : null}

      <div className={styles.transport}>
        <AudioTransport media={media} />
      </div>

      <div className={styles.wave}>
        <WaveformPanel
          media={media}
          waveformB64={state.waveformB64}
          duration={state.duration}
        />
      </div>

      <div className={styles.segments}>
        <SegmentList
          key={`segments-${String(snapshot.generation)}`}
          store={controller.editor}
          media={media}
          disabled={editingDisabled}
          onUpdateSegment={handleUpdateSegment}
          duration={state.duration}
        />
      </div>

      {metadataUiEnabled(snapshot.features) ? (
        <SceneReviewForm
          key={`review-${String(snapshot.generation)}`}
          value={snapshot.review}
          taxonomy={taxonomy}
          disabled={editingDisabled}
          writeEnabled={reviewWriteEnabled(snapshot.features)}
          onChange={handleUpdateReview}
        />
      ) : null}

      <div className={styles.actions}>
        <Button
          type="primary"
          onClick={() => void controller.flush({ manual: true })}
          disabled={
            editingDisabled ||
            (snapshot.dirtyCount === 0 &&
              !snapshot.reviewDirty &&
              snapshot.pendingCount === 0 &&
              snapshot.pendingTimeCount === 0 &&
              snapshot.unconfirmedOperationIds.length === 0)
          }
          data-testid="workspace-save"
        >
          Save draft
        </Button>
        <Button type="link" href="/" onClick={backToLegacy}>
          Back to classic workspace
        </Button>
      </div>
    </div>
  );
}

export default function WorkspacePreview(): React.JSX.Element {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  // Bumping this re-runs the whole load effect: new controller + media +
  // page snapshot. Used when recovery lands on a different assignment so
  // title/audio/waveform/editor all switch together (U01).
  const [reloadKey, setReloadKey] = useState(0);
  const cancelledRef = useRef(false);

  useEffect(() => {
    cancelledRef.current = false;
    let controller: PersistenceController | null = null;
    let media: MediaController | null = null;
    const aborter = new AbortController();
    setState({ kind: "loading" });

    async function load(): Promise<void> {
      try {
        const user = await apiGet<{
          user?: unknown;
          session?: {
            heartbeat_seconds?: unknown;
            idle_warning_seconds?: unknown;
            offline_draft_retention_days?: unknown;
            idle_expires_at?: unknown;
            absolute_expires_at?: unknown;
          } | null;
        }>("/api/current-user", {
          signal: aborter.signal,
        });
        // `/api/current-user` returns the username as a plain string.
        const rawUser = (user as { user?: unknown }).user;
        const username =
          typeof rawUser === "string"
            ? rawUser
            : typeof (rawUser as { username?: unknown } | null)?.username === "string"
              ? ((rawUser as { username?: string }).username as string)
              : "";
        if (!username) {
          if (!cancelledRef.current) {
            setState({ kind: "empty", username: "", detail: "No annotator session." });
          }
          return;
        }
        // Real server session metadata (R07): heartbeat cadence, warning
        // lead time, draft retention and the idle/absolute deadlines flow
        // into the session service instead of null defaults.
        const serverSession = (user as { session?: unknown }).session;
        const sessionMeta: Partial<SessionConfig> =
          serverSession && typeof serverSession === "object"
            ? {
                heartbeatSeconds: Number(
                  (serverSession as Record<string, unknown>).heartbeat_seconds,
                ),
                idleWarningSeconds: Number(
                  (serverSession as Record<string, unknown>).idle_warning_seconds,
                ),
                offlineDraftRetentionDays: Number(
                  (serverSession as Record<string, unknown>).offline_draft_retention_days,
                ),
                idleExpiresAt:
                  typeof (serverSession as Record<string, unknown>).idle_expires_at === "string"
                    ? ((serverSession as Record<string, string>).idle_expires_at as string)
                    : null,
                absoluteExpiresAt:
                  typeof (serverSession as Record<string, unknown>).absolute_expires_at ===
                  "string"
                    ? ((serverSession as Record<string, string>).absolute_expires_at as string)
                    : null,
              }
            : {};
        const fresh = await fetchFreshAssignment(username, aborter.signal);
        if (!fresh) {
          if (!cancelledRef.current) {
            setState({
              kind: "empty",
              username,
              detail: "No claimed task. Claim one in the classic workspace first.",
            });
          }
          return;
        }
        if (cancelledRef.current || aborter.signal.aborted) return;

        const { context } = fresh;
        // Audio attach happens in a WorkspaceBody effect (after the
        // transport binds the page-rendered element); never here, or the
        // controller would create an unreachable fallback instance.
        media = new MediaController();
        controller = new PersistenceController({
          sender: (body) =>
            apiSend<SaveResult>("/api/assignment/current", "PATCH", body),
          sessionFactory: (hooks) =>
            new SessionService(
              sessionMeta,
              {
                persist: hooks.persist,
                hasDirty: hooks.hasDirty,
                onFrozen: hooks.onFrozen,
                onOnline: hooks.onOnline,
              },
              { visibilityHidden: () => document.hidden },
            ),
        });
        await controller.attachAssignment(context, {
          ...(typeof sessionMeta.offlineDraftRetentionDays === "number"
            ? { retentionDays: sessionMeta.offlineDraftRetentionDays }
            : {}),
        });
        if (cancelledRef.current || aborter.signal.aborted) {
          controller.dispose();
          media.dispose();
          controller = null;
          media = null;
          return;
        }
        controller.attachSession(sessionMeta, username);
        controller.start();
        const ready = controller;
        const readyMedia = media;
        setState({
          kind: "ready",
          controller: ready,
          media: readyMedia,
          username,
          filename: fresh.filename,
          taskId: fresh.taskId,
          duration: fresh.duration,
          waveformB64: fresh.waveformB64,
          taxonomy: fresh.taxonomy,
        });
      } catch (error) {
        if (isAbortError(error) || cancelledRef.current) return;
        setState({
          kind: "failed",
          message: error instanceof Error ? error.message : "Could not load the assignment.",
        });
      }
    }

    void load();
    return () => {
      cancelledRef.current = true;
      aborter.abort();
      controller?.dispose();
      media?.dispose();
    };
  }, [reloadKey]);

  return (
    <RoleTheme role="annotator">
      <AnnotatorShell
        taskSlot={state.kind === "ready" ? state.filename : undefined}
        userEntry={
          state.kind === "ready" || state.kind === "empty" ? state.username : undefined
        }
      >
        {state.kind === "loading" ? (
          <div className={styles.state} role="status">
            Loading assignment…
          </div>
        ) : state.kind === "failed" ? (
          <div className={styles.state} role="alert" data-testid="workspace-load-error">
            <p>{state.message}</p>
            <p className={styles.actions}>
              {/* W07: a transient read failure keeps an explicit retry that
                  re-runs the current load generation. */}
              <Button
                type="primary"
                onClick={() => setReloadKey((key) => key + 1)}
                data-testid="workspace-load-retry"
              >
                Try again
              </Button>
              <Button type="link" href="/">
                Back to classic workspace
              </Button>
            </p>
          </div>
        ) : state.kind === "empty" ? (
          <div className={styles.state} role="status" data-testid="workspace-empty">
            <p>{state.detail}</p>
            <p>
              <a href="/">Back to classic workspace</a>
            </p>
          </div>
        ) : (
          <WorkspaceBody
            state={state}
            onAssignmentReplaced={() => setReloadKey((key) => key + 1)}
          />
        )}
      </AnnotatorShell>
    </RoleTheme>
  );
}
