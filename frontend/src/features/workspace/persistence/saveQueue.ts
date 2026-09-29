/**
 * Serial save queue (work package F, workspace/persistence).
 *
 * At most one in-flight write per assignment. Each operation freezes its
 * method/route/body under an immutable operation id before sending:
 *
 * - Input A in flight, then input B arrives: confirming A clears only the
 *   dirty rows still equal to A's snapshot; B stays queued and sends with
 *   the post-confirm revision.
 * - A committed-but-unacknowledged response (lost response) replays the
 *   ORIGINAL id and body — the queue never regenerates the operation, so the
 *   server applies it exactly once via its operations-table idempotency.
 * - 401 / deactivated-403 freeze immediately with no retry; 409 freezes for
 *   an explicit keep/export/discard decision and never auto-overwrites.
 *
 * The queue is framework-free and storage-agnostic: persistence (IndexedDB
 * outbox writes) and transport (PATCH) are injected, which keeps the order
 * "persist locally, then send the immutable request" assertable in tests.
 */

import { nextBackoff, shouldRetry } from "./backoff";
import { SAVE_METHOD, SAVE_ROUTE, deepFreeze, type SaveRequestBody, type SaveResult } from "./types";

export interface QueuedOperation {
  operationId: string;
  body: SaveRequestBody;
  /** Snapshot of the segment rows covered by this body (for ack diffing). */
  sentSegments: Array<Record<string, unknown>>;
  sentReview: unknown;
  enqueuedAt: number;
  attempts: number;
}

export interface QueueTransport {
  /** Write the frozen request to the outbox BEFORE it is sent. */
  stageOutbox: (op: QueuedOperation) => Promise<void>;
  /** Send the exact frozen body; resolves with the server revision. */
  send: (body: SaveRequestBody) => Promise<SaveResult>;
  /**
   * Confirm: delete the outbox entry and advance the stored revision.
   * Resolves `true` when the local confirmation transaction committed. A
   * `false` result keeps the operation accepted-but-unconfirmed so the writer
   * blocks later operations (Z01) until a retry confirms it.
   */
  confirm: (op: QueuedOperation, result: SaveResult) => Promise<boolean>;
  /** Record a failed attempt against the staged outbox entry. */
  bumpAttempt: (op: QueuedOperation) => Promise<void>;
}

export type QueueStatus = "idle" | "sending" | "waiting-retry" | "frozen";

export interface QueueSnapshot {
  status: QueueStatus;
  inFlightId: string | null;
  queuedIds: string[];
  attempts: number;
  frozenReason: string | null;
  /** Retry budget exhausted: parked until manual retry or reconnect. */
  exhausted: boolean;
  /** An operation the server accepted but whose local confirm failed. */
  unconfirmedId: string | null;
}

export type QueueListener = (snapshot: QueueSnapshot) => void;

export interface QueueDeps {
  now?: () => number;
  newOperationId?: () => string;
  setTimeout?: (callback: () => void, ms: number) => ReturnType<typeof setTimeout>;
  clearTimeout?: (handle: ReturnType<typeof setTimeout>) => void;
}

interface PendingInput {
  segments: Array<Record<string, unknown>>;
  review: unknown;
  includeReview: boolean;
  leaseToken: string;
  revision: number;
}

export class SaveQueue {
  private transport: QueueTransport;
  private deps: Required<QueueDeps>;
  private queue: QueuedOperation[] = [];
  private inFlight: QueuedOperation | null = null;
  private started = false;
  private frozenReason: string | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  /**
   * A backoff wait is pending. Tracked separately from the timer handle:
   * synchronous timer doubles (unit tests) run the callback before the
   * handle assignment lands, so handle-nullness alone cannot represent
   * "waiting". Only the timer callback, reset/freeze/dispose, or an
   * explicit manual/online signal clear this flag.
   */
  private retryScheduled = false;
  private pendingInput: PendingInput | null = null;
  private listeners = new Set<QueueListener>();
  private draining = false;
  private disposed = false;
  private replaySuspended = false;
  private idleResolvers: Array<() => void> = [];
  /**
   * The server accepted this operation but its local outbox confirmation
   * failed (Z01). The writer holds it here and refuses to send anything later
   * until it is confirmed (retry) or superseded by a successful replay of the
   * same ID. This is the single writer's confirmation phase.
   */
  private unconfirmed: QueuedOperation | null = null;
  private unconfirmedRevision = 0;
  /**
   * Retry budget exhausted: no further automatic sends until the user
   * retries explicitly or connectivity is re-established. Prevents the
   * drain loop from restarting the storm after the backoff gives up.
   */
  private retryExhausted = false;

  constructor(transport: QueueTransport, deps: QueueDeps = {}) {
    this.transport = transport;
    this.deps = {
      now: deps.now ?? (() => Date.now()),
      newOperationId: deps.newOperationId ?? (() => globalThis.crypto.randomUUID()),
      setTimeout: deps.setTimeout ?? ((callback, ms) => globalThis.setTimeout(callback, ms)),
      clearTimeout: deps.clearTimeout ?? ((handle) => globalThis.clearTimeout(handle)),
    };
  }

  subscribe(listener: QueueListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  getSnapshot(): QueueSnapshot {
    let status: QueueStatus = "idle";
    if (this.frozenReason) status = "frozen";
    else if (this.inFlight) status = "sending";
    else if (this.unconfirmed) status = "waiting-retry";
    else if (this.retryScheduled || this.queue.length > 0) status = "waiting-retry";
    return {
      status,
      inFlightId: this.inFlight?.operationId ?? null,
      queuedIds: this.queue.map((op) => op.operationId),
      attempts: this.inFlight?.attempts ?? 0,
      frozenReason: this.frozenReason,
      exhausted: this.retryExhausted,
      unconfirmedId: this.unconfirmed?.operationId ?? null,
    };
  }

  get frozen(): boolean {
    return this.frozenReason !== null;
  }

  /**
   * Latest editor state while a write is in flight (or queued). Coalesced
   * into the next operation; never mutates the in-flight body.
   */
  noteInput(input: PendingInput): void {
    if (this.frozenReason) return;
    this.pendingInput = { ...input };
  }

  takePendingInput(): PendingInput | null {
    const input = this.pendingInput;
    this.pendingInput = null;
    return input;
  }

  /** Whether an un-built input is waiting (peek without consuming). */
  hasPendingInput(): boolean {
    return this.pendingInput !== null;
  }

  /**
   * Build the next immutable operation from the latest noted input.
   * Returns null when there is nothing new to send.
   */
  buildNext(): QueuedOperation | null {
    const input = this.takePendingInput();
    if (!input) return null;
    const body = deepFreeze<SaveRequestBody>({
      lease_token: input.leaseToken,
      expected_revision: input.revision,
      operation_id: this.deps.newOperationId(),
      segments: input.segments.map((seg) => ({ ...(seg as object) })) as SaveRequestBody["segments"],
      ...(input.includeReview ? { scene_review: input.review as Exclude<SaveRequestBody["scene_review"], undefined> } : {}),
    });
    return {
      operationId: body.operation_id,
      body,
      sentSegments: input.segments.map((seg) => ({ ...seg })),
      sentReview: input.includeReview ? input.review : undefined,
      enqueuedAt: this.deps.now(),
      attempts: 0,
    };
  }

  /** Enqueue a pre-built operation (replay path reuses the original). */
  enqueue(op: QueuedOperation): void {
    if (this.frozenReason) return;
    if (this.queue.some((item) => item.operationId === op.operationId)) return;
    if (this.inFlight?.operationId === op.operationId) return;
    this.queue.push(op);
    this.emit();
    void this.drain();
  }

  /**
   * The confirmed operation clears only matching dirty state (the caller
   * diffs sent snapshot vs current values); the queue then continues with
   * whatever input arrived meanwhile, using the confirmed revision.
   *
   * If the local confirmation transaction fails, the operation STAYS in the
   * accepted-but-unconfirmed phase and the writer refuses later operations
   * until it is confirmed (Z01). Only a subsequent successful retry or a
   * same-id replay confirmation clears it.
   */
  async acknowledged(op: QueuedOperation, result: SaveResult): Promise<void> {
    if (this.inFlight?.operationId !== op.operationId) return;
    const confirmed = await this.transport.confirm(op, result);
    this.inFlight = null;
    if (!confirmed) {
      this.unconfirmed = op;
      this.unconfirmedRevision = result.revision;
      this.emit();
      return;
    }
    this.emit();
    void this.drain();
  }

  /**
   * Retry the local confirmation of the accepted-but-unconfirmed operation.
   * Resolves true when it committed (writer released), false otherwise. Does
   * NOT drain: the caller must run the rest of the recovery (outbox preflight
   * then newer input) so a persisted X is never overtaken by B (AB02).
   */
  async retryUnconfirmed(): Promise<boolean> {
    const op = this.unconfirmed;
    if (!op) return true;
    const confirmed = await this.transport.confirm(op, {
      revision: this.unconfirmedRevision,
    });
    if (confirmed) {
      this.unconfirmed = null;
      this.emit();
      return true;
    }
    return false;
  }

  /** Clear the unconfirmed phase for a specific id (e.g. replayed success). */
  markConfirmed(operationId: string): void {
    if (this.unconfirmed?.operationId === operationId) {
      this.unconfirmed = null;
      this.emit();
    }
  }

  get unconfirmedId(): string | null {
    return this.unconfirmed?.operationId ?? null;
  }

  async failed(op: QueuedOperation, error: { status: number; code?: string }): Promise<void> {
    if (this.inFlight?.operationId !== op.operationId) return;
    if (!shouldRetry(error.status, error.code)) {
      this.freeze(`no-retry:${error.status}${error.code ? `:${error.code}` : ""}`);
      return;
    }
    try {
      await this.transport.bumpAttempt(op);
    } catch {
      // Attempt bookkeeping must not break the retry loop.
    }
    op.attempts += 1;
    const backoff = nextBackoff(op.attempts);
    this.queue.unshift(op);
    this.inFlight = null;
    if (backoff.exhausted) {
      // Budget spent: park without a timer. Only an explicit manual retry
      // or a reconnect signal may drain again (see retryManual/notifyOnline).
      this.retryExhausted = true;
      this.emit();
      return;
    }
    this.emit();
    this.scheduleRetry(backoff.nextDelayMs ?? 0);
  }

  freeze(reason: string): void {
    if (this.frozenReason) return;
    this.frozenReason = reason;
    if (this.retryTimer !== null) {
      this.deps.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.retryScheduled = false;
    this.inFlight = null;
    this.emit();
  }

  /** Drain loop: strictly one in-flight PATCH at a time. */
  async drain(): Promise<void> {
    // Scheduling gates: never re-enter while draining, frozen, sending,
    // waiting on a backoff timer, or parked after budget exhaustion.
    // Only reset()/retryManual()/notifyOnline() reopen the exhausted path.
    if (
      this.draining ||
      this.disposed ||
      this.frozenReason ||
      this.inFlight ||
      this.unconfirmed || // Z01: an unconfirmed operation owns the writer
      this.retryScheduled ||
      this.retryExhausted ||
      this.replaySuspended
    ) {
      return;
    }
    this.draining = true;
    try {
      for (;;) {
        // Re-check suspension each iteration: a running drain must stop
        // shifting new operations once a replay takes the writer (T03).
        if (this.frozenReason || this.disposed || this.replaySuspended) return;
        const next = this.queue.shift() ?? null;
        if (!next) return;
        this.inFlight = next;
        this.emit();
        try {
          // Immutable order: stage the frozen request first, then send it.
          await this.transport.stageOutbox(next);
          const result = await this.transport.send(next.body);
          await this.acknowledged(next, result);
        } catch (error) {
          const status = Number((error as { status?: unknown }).status ?? 0);
          const code = (error as { code?: unknown }).code as string | undefined;
          if (this.inFlight?.operationId === next.operationId) {
            const safeStatus = Number.isFinite(status) ? status : 0;
            const detail = code === undefined ? { status: safeStatus } : { status: safeStatus, code };
            await this.failed(next, detail);
          }
          return;
        }
      }
    } finally {
      this.draining = false;
      // The backoff timer (or exhaustion park) owns the next attempt:
      // re-draining here would bypass the scheduled delay.
      if (
        !this.frozenReason &&
        !this.disposed &&
        !this.inFlight &&
        this.queue.length > 0 &&
        !this.retryScheduled &&
        !this.retryExhausted
      ) {
        void this.drain();
      }
      this.notifyIdleIfIdle();
      this.emit();
    }
  }

  start(): void {
    this.started = true;
    void this.drain();
  }

  get isStarted(): boolean {
    return this.started;
  }

  pendingCount(): number {
    return this.queue.length + (this.inFlight ? 1 : 0);
  }

  dispose(): void {
    this.disposed = true;
    if (this.retryTimer !== null) {
      this.deps.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.retryScheduled = false;
    this.listeners.clear();
  }

  /**
   * Explicit user retry (Save button / Ctrl+S): clears a parked exhaustion
   * and drains once. Automatic paths (autosave, enqueue) never call this,
   * so a spent budget stays silent until the user or the network acts.
   */
  retryManual(): void {
    if (this.disposed || this.frozenReason) return;
    this.retryExhausted = false;
    void this.drain();
  }

  /**
   * Replay reconciliation runs on this queue (single writer, N02/T03): pause
   * sending so the serial outbox replay is not raced by a queue drain, and
   * WAIT for any in-flight operation to settle before the caller takes the
   * writer. Without the wait, a late confirmation of the original op could
   * rewind a revision the replay already advanced.
   */
  async suspendForReplay(): Promise<void> {
    this.replaySuspended = true;
    if (this.retryTimer !== null) {
      this.deps.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.retryScheduled = false;
    await this.waitForIdle();
  }

  /** Resolve once there is no in-flight operation and no active drain. */
  waitForIdle(): Promise<void> {
    if (!this.inFlight && !this.draining) return Promise.resolve();
    return new Promise((resolve) => {
      this.idleResolvers.push(resolve);
    });
  }

  private notifyIdleIfIdle(): void {
    if (this.inFlight || this.draining) return;
    const resolvers = this.idleResolvers;
    this.idleResolvers = [];
    resolvers.forEach((resolve) => resolve());
  }

  /**
   * Resume after replay; replayed operations no longer need sending. Does NOT
   * auto-drain: the caller decides whether the writer may proceed, so a
   * barrier set after a failed replay is observed before any send (Z04).
   */
  resumeAfterReplay(replayedIds: Iterable<string>): void {
    const done = new Set(replayedIds);
    // Drop queued ops the replay covered, plus the current in-flight op if
    // it happens to be one of them (its late confirmation must not rewind).
    this.queue = this.queue.filter((op) => !done.has(op.operationId));
    if (this.inFlight && done.has(this.inFlight.operationId)) {
      this.inFlight = null;
    }
    this.replaySuspended = false;
    this.notifyIdleIfIdle();
    this.emit();
  }

  /** Explicitly allow the writer to continue after a successful replay. */
  continueAfterReplay(): void {
    this.replaySuspended = false;
    void this.drain();
  }

  /**
   * Release the replay suspension without draining. Used by the manual-save
   * reconcile (W03) so the caller can build exactly one new operation from
   * the pending input and drain it itself — no auto-enqueue race.
   */
  releaseReplay(replayedIds: Iterable<string>): void {
    const done = new Set(replayedIds);
    this.queue = this.queue.filter((op) => !done.has(op.operationId));
    if (this.inFlight && done.has(this.inFlight.operationId)) {
      this.inFlight = null;
    }
    this.replaySuspended = false;
  }

  /**
   * Connectivity restored: clears a parked exhaustion and drains pending
   * work. Frozen (auth/conflict) queues stay frozen.
   */
  notifyOnline(): void {
    if (this.disposed || this.frozenReason) return;
    this.retryExhausted = false;
    void this.drain();
  }

  /**
   * Drop every pending and in-flight operation (task switch). Late
   * confirmations for dropped ids are ignored by id comparison.
   */
  reset(): void {
    if (this.retryTimer !== null) {
      this.deps.clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    this.retryScheduled = false;
    this.queue = [];
    this.inFlight = null;
    this.pendingInput = null;
    this.frozenReason = null;
    this.retryExhausted = false;
    this.replaySuspended = false;
    this.unconfirmed = null;
    this.emit();
  }

  private scheduleRetry(delayMs: number): void {
    if (this.disposed || this.replaySuspended) return;
    if (this.retryTimer !== null) this.deps.clearTimeout(this.retryTimer);
    this.retryScheduled = true;
    this.retryTimer = this.deps.setTimeout(() => {
      this.retryTimer = null;
      this.retryScheduled = false;
      if (!this.disposed && !this.replaySuspended) void this.drain();
    }, delayMs);
  }

  private emit(): void {
    const snapshot = this.getSnapshot();
    this.listeners.forEach((listener) => {
      try {
        listener(snapshot);
      } catch {
        // Listener errors must never corrupt queue state.
      }
    });
  }
}

export { SAVE_METHOD, SAVE_ROUTE };
