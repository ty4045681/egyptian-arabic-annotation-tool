/**
 * Session protocol unit tests (R07): trusted-activity gating, heartbeat
 * activity flag semantics (including restore-on-failure), unauthorized
 * handling, and single-timer lifecycle across start/dispose cycles.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  defaultReloginTarget,
  heartbeatIntervalMs,
  isTrustedActivity,
  SessionService,
  type FetchLike,
} from "../features/auth/sessionService";

function okFetch(body: unknown = {}): FetchLike {
  return (() =>
    Promise.resolve({
      status: 200,
      ok: true,
      json: () => Promise.resolve(body),
    })) as FetchLike;
}

describe("trusted activity", () => {
  const button = { closest: (sel: string) => (sel.includes("button") ? {} : null) };
  const audio = { closest: () => ({}) };
  const other = { closest: () => null };

  it("accepts real pointer/keyboard/input, rejects the rest", () => {
    expect(isTrustedActivity({ type: "pointerdown", isTrusted: true })).toBe(true);
    expect(isTrustedActivity({ type: "keydown", isTrusted: true })).toBe(true);
    expect(isTrustedActivity({ type: "input", isTrusted: true })).toBe(true);
    // Legacy parity: a missing isTrusted flag is not a synthetic verdict.
    expect(isTrustedActivity({ type: "input" })).toBe(true);
    expect(isTrustedActivity({ type: "keydown", isTrusted: false })).toBe(false);
    expect(isTrustedActivity({ type: "scroll", isTrusted: true })).toBe(false);
    expect(isTrustedActivity(null)).toBe(false);
  });

  it("counts clicks on controls and continuous listening (legacy parity)", () => {
    expect(isTrustedActivity({ type: "click", isTrusted: true, target: button })).toBe(true);
    expect(isTrustedActivity({ type: "click", isTrusted: true, target: other })).toBe(false);
    expect(isTrustedActivity({ type: "play", isTrusted: true, target: audio })).toBe(true);
    expect(isTrustedActivity({ type: "seeked", isTrusted: true, target: audio })).toBe(true);
    expect(isTrustedActivity({ type: "play", isTrusted: true, target: other })).toBe(false);
    expect(isTrustedActivity({ type: "play", isTrusted: false, target: audio })).toBe(false);
  });

  it("clamps heartbeat cadence like the legacy client", () => {
    expect(heartbeatIntervalMs({ heartbeatSeconds: 5 })).toBe(15000);
    expect(heartbeatIntervalMs({ heartbeatSeconds: 30 })).toBe(30000);
  });

  it("builds the legacy reason redirect", () => {
    expect(defaultReloginTarget("idle_timeout")).toBe("/login.html?reason=idle_timeout");
  });
});

describe("heartbeat activity flag", () => {
  it("sends activity:true once after trusted input, then clears it", async () => {
    const seen: boolean[] = [];
    const service = new SessionService(
      { heartbeatSeconds: 30 },
      {},
      {
        fetchImpl: ((_url: string, init?: { body?: string }) => {
          seen.push(JSON.parse(String(init?.body ?? "{}")).activity === true);
          return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({}) });
        }) as FetchLike,
      },
    );
    await service.beat();
    expect(seen).toEqual([false]);
    service.noteEvent({ type: "keydown", isTrusted: true });
    await service.beat();
    expect(seen).toEqual([false, true]);
    await service.beat();
    expect(seen).toEqual([false, true, false]);
    service.dispose();
  });

  it("restores the activity flag when the beat fails", async () => {
    let fail = true;
    const seen: boolean[] = [];
    const service = new SessionService(
      { heartbeatSeconds: 30 },
      {},
      {
        fetchImpl: ((_url: string, init?: { body?: string }) => {
          seen.push(JSON.parse(String(init?.body ?? "{}")).activity === true);
          if (fail) return Promise.reject(new Error("offline"));
          return Promise.resolve({ status: 200, ok: true, json: () => Promise.resolve({}) });
        }) as FetchLike,
      },
    );
    service.noteEvent({ type: "input", isTrusted: true });
    expect(await service.beat()).toBe(false);
    fail = false;
    expect(await service.beat()).toBe(true);
    // The activity lost to the failed beat is retried, not dropped.
    expect(seen).toEqual([true, true]);
    service.dispose();
  });

  it("synthetic events never mark activity", async () => {
    const seen: boolean[] = [];
    const service = new SessionService({ heartbeatSeconds: 30 }, {}, { fetchImpl: okFetch() });
    service.noteEvent({ type: "keydown", isTrusted: false });
    service.noteEvent({ type: "scroll", isTrusted: true });
    await service.beat();
    expect(service.snapshot.activitySinceHeartbeat).toBe(false);
    expect(seen).toEqual([]);
    service.dispose();
  });

  it("401 with dirty work freezes instead of navigating away", async () => {
    let navigated = "";
    const service = new SessionService(
      { heartbeatSeconds: 30 },
      {
        hasDirty: () => true,
        onRelogin: (code) => {
          navigated = code;
        },
      },
      {
        fetchImpl: (() =>
          Promise.resolve({
            status: 401,
            ok: false,
            json: () => Promise.resolve({ code: "not_authenticated" }),
          })) as FetchLike,
      },
    );
    expect(await service.beat()).toBe(false);
    expect(service.snapshot.frozen).toBe(true);
    expect(navigated).toBe("");
    service.dispose();
  });
});

describe("heartbeat lifecycle", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps a single timer across repeated starts and stops it on dispose", async () => {
    const fetchImpl = okFetch();
    const service = new SessionService({ heartbeatSeconds: 30 }, {}, { fetchImpl });
    service.start();
    service.start();
    expect(service.hasTimer()).toBe(true);
    const timers = vi.getTimerCount();
    expect(timers).toBe(1);
    service.dispose();
    expect(service.hasTimer()).toBe(false);
    // A disposed service never schedules again.
    service.start();
    expect(service.hasTimer()).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });
});
