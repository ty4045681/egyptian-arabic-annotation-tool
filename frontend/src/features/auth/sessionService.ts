/**
 * Session protocol service (work package F, features/auth).
 *
 * Pure-protocol extraction of static/annotator-session.js: trusted-activity
 * semantics, heartbeat request/response handling, and freeze decisions.
 * No DOM access here — overlay rendering, navigation and network-flag UI are
 * delivered through callbacks so React (or tests) can bind them. Exactly one
 * timer per live service; start() is idempotent and dispose() tears all
 * resources down, so React StrictMode remounts cannot create a second
 * heartbeat.
 */

export type FreezeCode =
  | "session_replaced"
  | "account_deactivated"
  | "idle_timeout"
  | "absolute_timeout"
  | "not_authenticated";

export interface SessionConfig {
  heartbeatSeconds: number;
  idleWarningSeconds: number;
  offlineDraftRetentionDays: number;
  idleExpiresAt: string | null;
  absoluteExpiresAt: string | null;
}

export interface SessionHooks {
  /** Persist the local draft before navigating away or freezing. */
  persist?: () => Promise<void>;
  hasDirty?: () => boolean;
  onFrozen?: (code: FreezeCode) => void;
  onExport?: () => Promise<void> | void;
  onOnline?: () => Promise<void> | void;
  /** Defaults to full navigation to /login.html?reason=<code>. */
  onRelogin?: (code: FreezeCode) => void;
}

export interface BeatResult {
  ok: boolean;
  /** Activity flag restored because the beat failed (must be retried). */
  activityRestored: boolean;
}

const TRUSTED_ACTIVITY = new Set(["pointerdown", "keydown", "input"]);

/**
 * Pure equivalent of annotator-session.js trustedActivity().
 * Presence must never extend idle/absolute deadlines on synthetic events.
 * Parity notes: clicks on buttons/links/activity-marked elements count
 * (they express intent even though "click" is not an editing event), and
 * continuous listening counts via play/seeked on the audio element. The
 * legacy code required id="audio"; here any audio element qualifies
 * because the workspace owns exactly one audio instance.
 */
export function isTrustedActivity(
  event:
    | {
        type: string;
        isTrusted?: boolean;
        target?: { closest?: (selectors: string) => unknown } | null;
      }
    | null
    | undefined,
): boolean {
  if (!event || event.isTrusted === false) return false;
  if (TRUSTED_ACTIVITY.has(event.type)) return true;
  const target = event.target;
  if (!target || typeof target.closest !== "function") return false;
  if (event.type === "click" && target.closest("button, a, [data-activity]")) return true;
  if ((event.type === "play" || event.type === "seeked") && target.closest("audio")) {
    return true;
  }
  return false;
}

/** Heartbeat interval bounds mirror the legacy clamp (min 15s). */
export function heartbeatIntervalMs(config: Pick<SessionConfig, "heartbeatSeconds">): number {
  const seconds = Math.max(15, Number(config.heartbeatSeconds) || 30);
  return seconds * 1000;
}

export function defaultReloginTarget(code: FreezeCode): string {
  return "/login.html" + (code ? `?reason=${encodeURIComponent(code)}` : "");
}

export interface FetchLike {
  (url: string, init?: { method?: string; headers?: Record<string, string>; body?: string }): Promise<{
    status: number;
    ok: boolean;
    json: () => Promise<unknown>;
  }>;
}

export class SessionService {
  private config: SessionConfig;
  private hooks: SessionHooks;
  private fetchImpl: FetchLike;
  private visibilityHidden: () => boolean;
  private timer: ReturnType<typeof setInterval> | null = null;
  private beatPromise: Promise<boolean> | null = null;
  private activitySinceHeartbeat = false;
  private frozen = false;
  private overlayCode = "";
  private disposed = false;
  /**
   * Network/session reachability (W09): the last heartbeat result. `null`
   * before the first beat; true after a successful beat; false after a
   * network/catch failure. Distinct from the freeze state so the UI can show
   * "waiting for connection" without pretending the session expired.
   */
  private onlineState: boolean | null = null;
  private networkListeners = new Set<(online: boolean | null) => void>();

  constructor(
    session: Partial<SessionConfig> | null | undefined,
    hooks: SessionHooks = {},
    deps: { fetchImpl?: FetchLike; visibilityHidden?: () => boolean } = {},
  ) {
    this.config = {
      heartbeatSeconds: Number(session?.heartbeatSeconds) || 30,
      idleWarningSeconds: Number(session?.idleWarningSeconds) || 120,
      offlineDraftRetentionDays: Number(session?.offlineDraftRetentionDays) || 7,
      idleExpiresAt: session?.idleExpiresAt ?? null,
      absoluteExpiresAt: session?.absoluteExpiresAt ?? null,
    };
    this.hooks = hooks;
    this.fetchImpl = deps.fetchImpl ?? ((globalThis as unknown as { fetch: FetchLike }).fetch.bind(globalThis));
    this.visibilityHidden = deps.visibilityHidden ?? (() => false);
  }

  get snapshot(): {
    frozen: boolean;
    overlayCode: string;
    activitySinceHeartbeat: boolean;
    heartbeatMs: number;
  } {
    return {
      frozen: this.frozen,
      overlayCode: this.overlayCode,
      activitySinceHeartbeat: this.activitySinceHeartbeat,
      heartbeatMs: heartbeatIntervalMs(this.config),
    };
  }

  get retentionDays(): number {
    return this.config.offlineDraftRetentionDays;
  }

  /** Latest heartbeat reachability: null unknown, true online, false failed. */
  get isOnline(): boolean | null {
    return this.onlineState;
  }

  subscribeNetwork(listener: (online: boolean | null) => void): () => void {
    this.networkListeners.add(listener);
    return () => {
      this.networkListeners.delete(listener);
    };
  }

  private setOnlineState(value: boolean): void {
    if (this.onlineState === value) return;
    this.onlineState = value;
    this.networkListeners.forEach((listener) => {
      try {
        listener(value);
      } catch {
        // Listener errors must not corrupt session state.
      }
    });
  }

  markActivity(): void {
    this.activitySinceHeartbeat = true;
  }

  noteEvent(
    event:
      | {
          type: string;
          isTrusted?: boolean;
          target?: { closest?: (selectors: string) => unknown } | null;
        }
      | null
      | undefined,
  ): void {
    if (isTrustedActivity(event)) this.markActivity();
  }

  writesFrozen(): boolean {
    return this.frozen;
  }

  freezeWrites(code: FreezeCode): void {
    if (this.frozen) return;
    this.frozen = true;
    this.overlayCode = code;
    this.hooks.onFrozen?.(code);
  }

  /** Idempotent: repeated starts (StrictMode remount) keep a single timer. */
  start(): void {
    if (this.disposed || this.frozen) return;
    this.stopTimer();
    this.timer = setInterval(() => {
      void this.beat();
    }, heartbeatIntervalMs(this.config));
  }

  stopTimer(): void {
    if (this.timer !== null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  hasTimer(): boolean {
    return this.timer !== null;
  }

  async persistLocal(): Promise<void> {
    try {
      await this.hooks.persist?.();
    } catch {
      // Local persist failures are surfaced by the persistence controller's
      // own status; the session layer must never throw out of freeze paths.
    }
  }

  private readHasDirty(explicit?: boolean): boolean {
    if (explicit !== undefined) return explicit;
    try {
      return Boolean(this.hooks.hasDirty?.());
    } catch {
      return false;
    }
  }

  /**
   * Mirrors handleUnauthorized(): persist first, then freeze with export when
   * there is unsaved work; only navigate away when there is nothing to lose.
   * Returns the overlay code (empty string when a navigation happened).
   */
  async handleUnauthorized(
    data: { code?: string; redirect?: string } | null | undefined,
    options: { hasDirty?: boolean } = {},
  ): Promise<FreezeCode | ""> {
    const code = ((data?.code ?? "not_authenticated") as FreezeCode) || "not_authenticated";
    const hasDirty = this.readHasDirty(options.hasDirty);
    await this.persistLocal();
    if (
      code === "session_replaced" ||
      code === "account_deactivated" ||
      code === "idle_timeout" ||
      code === "absolute_timeout"
    ) {
      this.freezeWrites(code);
      return code;
    }
    if (hasDirty) {
      this.freezeWrites("not_authenticated");
      return code;
    }
    const relogin = this.hooks.onRelogin ?? ((c: FreezeCode) => {
      globalThis.location.href = defaultReloginTarget(c);
    });
    relogin(code);
    return "";
  }

  /**
   * One heartbeat round. Concurrent callers share the in-flight promise.
   * Failed beats restore the activity flag so presence is not silently lost;
   * presence itself never extends idle/absolute deadlines (server-owned).
   */
  beat(): Promise<boolean> {
    if (this.frozen) return Promise.resolve(false);
    if (this.beatPromise) return this.beatPromise;
    const activity = !this.visibilityHidden() && this.activitySinceHeartbeat;
    if (activity) this.activitySinceHeartbeat = false;
    this.beatPromise = (async (): Promise<boolean> => {
      try {
        const response = await this.fetchImpl("/api/session/heartbeat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ activity }),
        });
        let data: { code?: string } = {};
        try {
          data = (await response.json()) as { code?: string };
        } catch {
          data = {};
        }
        if (response.status === 401 || (response.status === 403 && data.code === "account_deactivated")) {
          await this.handleUnauthorized(data);
          return false;
        }
        if (!response.ok) {
          if (activity) this.activitySinceHeartbeat = true;
          // A served 5xx still proves reachability; only network failures
          // below mark the client offline.
          if (response.status >= 500) this.setOnlineState(true);
          return false;
        }
        this.setOnlineState(true);
        return true;
      } catch {
        if (activity) this.activitySinceHeartbeat = true;
        // Network-level failure: surface an offline/network state (W09).
        this.setOnlineState(false);
        return false;
      } finally {
        this.beatPromise = null;
      }
    })();
    return this.beatPromise;
  }

  /** Online after offline: re-validate the session before replaying saves. */
  async handleOnline(): Promise<boolean> {
    const active = await this.beat();
    if (active) {
      try {
        await this.hooks.onOnline?.();
      } catch {
        return false;
      }
    }
    return active;
  }

  async exportDraft(): Promise<void> {
    await this.persistLocal();
    await this.hooks.onExport?.();
  }

  relogin(): void {
    const relogin = this.hooks.onRelogin ?? ((c: FreezeCode) => {
      globalThis.location.href = defaultReloginTarget(c);
    });
    relogin((this.overlayCode as FreezeCode) || "not_authenticated");
  }

  dispose(): void {
    this.disposed = true;
    this.stopTimer();
    this.beatPromise = null;
  }
}
