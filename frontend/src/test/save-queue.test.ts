/**
 * Pure-logic tests for the serial save queue, backoff policy and stored
 * record validation. No DOM, no network: transport and timers are faked.
 * Browser tests cover the same behaviors end to end; these pin the state
 * machine transitions (ack never clears newer input, freeze/park rules).
 */
import { describe, expect, it, vi } from "vitest";
import { nextBackoff, shouldRetry } from "../features/workspace/persistence/backoff";
import {
  validateStoredDraft,
  validateStoredOutbox,
} from "../features/workspace/persistence/offlineAdapter";
import {
  SaveQueue,
  type QueuedOperation,
  type QueueTransport,
} from "../features/workspace/persistence/saveQueue";
import type {
  SaveRequestBody,
  SaveResult,
} from "../features/workspace/persistence/types";

function body(overrides: Partial<SaveRequestBody> = {}): SaveRequestBody {
  return {
    lease_token: "lease-1",
    expected_revision: 0,
    operation_id: `op-${Math.random().toString(36).slice(2)}`,
    segments: [],
    ...overrides,
  };
}

function operation(id: string, revision = 0): QueuedOperation {
  const request = body({ operation_id: id, expected_revision: revision });
  return {
    operationId: id,
    body: request,
    sentSegments: [],
    sentReview: undefined,
    enqueuedAt: Date.now(),
    attempts: 0,
  };
}

function immediateDeps() {
  let n = 0;
  return {
    now: () => 1000,
    newOperationId: () => `op-new-${(n += 1)}`,
    setTimeout: ((callback: () => void) => {
      callback();
      return 0 as unknown as ReturnType<typeof setTimeout>;
    }) as (callback: () => void, ms: number) => ReturnType<typeof setTimeout>,
    clearTimeout: () => undefined,
  };
}

function fakeTransport(behavior: {
  onSend?: (body: SaveRequestBody) => Promise<SaveResult>;
} = {}): QueueTransport & { sent: SaveRequestBody[]; staged: string[]; confirmed: string[] } {
  const sent: SaveRequestBody[] = [];
  const staged: string[] = [];
  const confirmed: string[] = [];
  return {
    sent,
    staged,
    confirmed,
    stageOutbox: (op) => {
      staged.push(op.operationId);
      return Promise.resolve();
    },
    send: (request) => {
      sent.push(request);
      if (behavior.onSend) return behavior.onSend(request);
      return Promise.resolve({ revision: 1 });
    },
    confirm: (op) => {
      confirmed.push(op.operationId);
      return Promise.resolve(true);
    },
    bumpAttempt: () => Promise.resolve(),
  };
}

describe("save queue ordering", () => {
  it("sends one operation at a time and continues with the queued one", async () => {
    const transport = fakeTransport();
    const queue = new SaveQueue(transport, immediateDeps());
    let release!: () => void;
    const gate = new Promise<SaveResult>((resolve) => {
      release = () => resolve({ revision: 1 });
    });
    transport.send = ((request: SaveRequestBody) => {
      transport.sent.push(request);
      if (request.operation_id === "op-a") return gate;
      return Promise.resolve({ revision: 2 });
    }) as QueueTransport["send"];
    queue.enqueue(operation("op-a"));
    queue.enqueue(operation("op-b"));
    await Promise.resolve();
    await Promise.resolve();
    expect(queue.getSnapshot().inFlightId).toBe("op-a");
    expect(queue.getSnapshot().queuedIds).toEqual(["op-b"]);
    release();
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(transport.sent.map((s) => s.operation_id)).toEqual(["op-a", "op-b"]);
    expect(queue.getSnapshot().status).toBe("idle");
  });

  it("buildNext freezes the body and mints a fresh operation id", () => {
    const queue = new SaveQueue(fakeTransport(), immediateDeps());
    queue.noteInput({
      segments: [{ id: 1, text: "a" }],
      review: undefined,
      includeReview: false,
      leaseToken: "lease-1",
      revision: 3,
    });
    const first = queue.buildNext();
    expect(first).not.toBeNull();
    expect(Object.isFrozen(first?.body)).toBe(true);
    queue.noteInput({
      segments: [{ id: 1, text: "b" }],
      review: undefined,
      includeReview: false,
      leaseToken: "lease-1",
      revision: 3,
    });
    const second = queue.buildNext();
    expect(second?.operationId).not.toBe(first?.operationId);
    // The in-flight body is never mutated by newer input.
    expect(first?.body.segments).toEqual([{ id: 1, text: "a" }]);
  });

  it("replays the identical operation after a transient failure", async () => {
    let calls = 0;
    const transport = fakeTransport({
      onSend: () => {
        calls += 1;
        if (calls === 1) {
          const error = { status: 0 };
          return Promise.reject(error);
        }
        return Promise.resolve({ revision: 7 });
      },
    });
    const queue = new SaveQueue(transport, immediateDeps());
    queue.enqueue(operation("op-retry"));
    queue.start();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(transport.sent.length).toBe(2);
    expect(transport.sent[0]?.operation_id).toBe("op-retry");
    expect(transport.sent[1]?.operation_id).toBe("op-retry");
    expect(transport.sent[0]).toBe(transport.sent[1]);
  });

  it("freezes immediately on 401 and never retries", async () => {
    const transport = fakeTransport({
      onSend: () => Promise.reject({ status: 401, code: "not_authenticated" }),
    });
    const queue = new SaveQueue(transport, immediateDeps());
    const drain = vi.spyOn(queue, "drain");
    queue.enqueue(operation("op-401"));
    queue.start();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(transport.sent.length).toBe(1);
    expect(queue.getSnapshot().status).toBe("frozen");
    expect(queue.getSnapshot().frozenReason).toContain("401");
    drain.mockRestore();
  });

  it("freeze() stops the drain and reset() clears it", async () => {
    const transport = fakeTransport();
    const queue = new SaveQueue(transport, immediateDeps());
    queue.freeze("manual");
    queue.enqueue(operation("op-x"));
    queue.start();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(transport.sent.length).toBe(0);
    queue.reset();
    queue.enqueue(operation("op-y"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(transport.sent.map((s) => s.operation_id)).toEqual(["op-y"]);
  });
});

/** Drain all pending microtasks (the manual clock runs timer callbacks
 * synchronously; promise continuations still need real ticks). */
function flushMicrotasks(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}
function manualClock() {
  let now = 0;
  let seq = 0;
  let opSeq = 0;
  const pending = new Map<number, { at: number; callback: () => void }>();
  return {
    now: () => now,
    newOperationId: () => `op-manual-${(opSeq += 1)}`,
    setTimeout: ((callback: () => void, ms: number) => {
      const handle = (seq += 1);
      pending.set(handle, { at: now + ms, callback });
      return handle as unknown as ReturnType<typeof setTimeout>;
    }) as (callback: () => void, ms: number) => ReturnType<typeof setTimeout>,
    clearTimeout: ((handle: unknown) => {
      pending.delete(handle as number);
    }) as (handle: ReturnType<typeof setTimeout>) => void,
    advance(ms: number) {
      const target = now + ms;
      for (;;) {
        let next: number | null = null;
        let nextAt = Number.POSITIVE_INFINITY;
        for (const [handle, entry] of pending) {
          if (entry.at <= target && entry.at < nextAt) {
            next = handle;
            nextAt = entry.at;
          }
        }
        if (next === null) break;
        const entry = pending.get(next);
        pending.delete(next);
        if (!entry) continue;
        now = entry.at;
        entry.callback();
      }
      now = target;
    },
    pendingCount() {
      return pending.size;
    },
  };
}

describe("retry scheduling (R01)", () => {
  it("waits the full backoff before the first retry", async () => {
    const clock = manualClock();
    const transport = fakeTransport({
      onSend: () => Promise.reject({ status: 503 }),
    });
    const queue = new SaveQueue(transport, clock);
    queue.enqueue(operation("op-storm"));
    queue.start();
    await flushMicrotasks();
    expect(transport.sent.length).toBe(1);
    clock.advance(400);
    await flushMicrotasks();
    expect(transport.sent.length).toBe(1);
    clock.advance(1600);
    await flushMicrotasks();
    expect(transport.sent.length).toBe(2);
  });

  it("stops after the attempt budget and stays silent", async () => {
    const clock = manualClock();
    const transport = fakeTransport({
      onSend: () => Promise.reject({ status: 503 }),
    });
    const queue = new SaveQueue(transport, clock);
    queue.enqueue(operation("op-budget"));
    queue.start();
    for (let step = 0; step < 40; step += 1) {
      clock.advance(5000);
      await flushMicrotasks();
    }
    expect(transport.sent.length).toBe(8);
    expect(queue.getSnapshot().exhausted).toBe(true);
    // Autosave-style automatic drains must not restart the storm.
    queue.noteInput({
      segments: [{ id: 1, text: "more" }],
      review: undefined,
      includeReview: false,
      leaseToken: "lease-1",
      revision: 0,
    });
    await queue.drain();
    clock.advance(60000);
    await flushMicrotasks();
    expect(transport.sent.length).toBe(8);
  });

  it("manual retry and reconnect reopen a parked budget", async () => {
    const clock = manualClock();
    let live = false;
    const transport = fakeTransport({
      onSend: () =>
        live ? Promise.resolve({ revision: 1 }) : Promise.reject({ status: 0 }),
    });
    const queue = new SaveQueue(transport, clock);
    queue.enqueue(operation("op-park"));
    queue.start();
    for (let step = 0; step < 40; step += 1) {
      clock.advance(5000);
      await flushMicrotasks();
    }
    expect(queue.getSnapshot().exhausted).toBe(true);
    const parked = transport.sent.length;
    live = true;
    queue.retryManual();
    await flushMicrotasks();
    expect(transport.sent.length).toBeGreaterThan(parked);
  });

  it("dispose cancels a pending retry", async () => {
    const clock = manualClock();
    const transport = fakeTransport({
      onSend: () => Promise.reject({ status: 503 }),
    });
    const queue = new SaveQueue(transport, clock);
    queue.enqueue(operation("op-gone"));
    queue.start();
    await Promise.resolve();
    expect(transport.sent.length).toBe(1);
    queue.dispose();
    clock.advance(30000);
    await flushMicrotasks();
    expect(transport.sent.length).toBe(1);
  });

  it("A in flight + input B: B sends after A confirms (R04 regression)", async () => {    const clock = manualClock();
    let releaseA!: (value: SaveResult) => void;
    const gateA = new Promise<SaveResult>((resolve) => {
      releaseA = resolve;
    });
    const transport = fakeTransport();
    let gatedId = "";
    transport.send = (async (request: SaveRequestBody) => {
      transport.sent.push(request);
      if (request.operation_id === gatedId) return gateA;
      return { revision: 2 };
    }) as QueueTransport["send"];
    const queue = new SaveQueue(transport, clock);
    // Op A built from noted input, like controller.flush() does.
    queue.noteInput({
      segments: [{ id: 1, text: "A" }],
      review: undefined,
      includeReview: false,
      leaseToken: "lease-1",
      revision: 0,
    });
    const opA = queue.buildNext();
    expect(opA).not.toBeNull();
    gatedId = (opA as QueuedOperation).operationId;
    queue.enqueue(opA as QueuedOperation);
    queue.start();
    await flushMicrotasks();
    expect(queue.getSnapshot().inFlightId).toBe(gatedId);
    // B arrives mid-flight and is only noted (controller.afterEdit path).
    queue.noteInput({
      segments: [{ id: 1, text: "A plus B" }],
      review: undefined,
      includeReview: false,
      leaseToken: "lease-1",
      revision: 0,
    });
    releaseA({ revision: 1 });
    await flushMicrotasks();
    // The queue-level continuation used by onQueueChanged: idle + empty +
    // noted input must produce op B.
    const next = queue.buildNext();
    expect(next).not.toBeNull();
    queue.enqueue(next as QueuedOperation);
    await flushMicrotasks();
    const sentIds = transport.sent.map((s) => s.operation_id);
    expect(sentIds).toEqual([gatedId, (next as QueuedOperation).operationId]);
    expect(sentIds[0]).not.toBe(sentIds[1]);
  });
});

describe("backoff policy", () => {
  it("retries offline and 5xx/408/429, never auth or conflicts", () => {
    expect(shouldRetry(0)).toBe(true);
    expect(shouldRetry(500)).toBe(true);
    expect(shouldRetry(503)).toBe(true);
    expect(shouldRetry(408)).toBe(true);
    expect(shouldRetry(429)).toBe(true);
    expect(shouldRetry(401)).toBe(false);
    expect(shouldRetry(403, "account_deactivated")).toBe(false);
    expect(shouldRetry(409)).toBe(false);
    expect(shouldRetry(400)).toBe(false);
  });

  it("caps delays and exhausts after the attempt budget", () => {
    expect(nextBackoff(0).nextDelayMs).toBe(1000);
    expect(nextBackoff(5).nextDelayMs).toBe(8000);
    const last = nextBackoff(8);
    expect(last.exhausted).toBe(true);
    expect(last.nextDelayMs).toBeNull();
    expect(nextBackoff(7).exhausted).toBe(false);
  });
});

describe("stored record validation", () => {
  it("accepts a well-formed draft and rejects corrupt shapes", () => {
    const good = {
      username: "u",
      task_id: "t",
      segments: [],
      server_revision: 1,
      lease_token: "l",
    };
    expect(validateStoredDraft(good).ok).toBe(true);
    expect(validateStoredDraft(null).ok).toBe(false);
    expect(validateStoredDraft({ ...good, segments: "x" }).ok).toBe(false);
    expect(validateStoredDraft({ ...good, server_revision: "1" }).ok).toBe(false);
  });

  it("accepts a well-formed outbox item and rejects mismatches", () => {
    const item = {
      operation_id: "op-1",
      username: "u",
      task_id: "t",
      route: "/api/assignment/current",
      method: "PATCH",
      body: {
        operation_id: "op-1",
        lease_token: "l",
        expected_revision: 0,
        segments: [],
      },
      created_at: new Date().toISOString(),
      attempts: 0,
    };
    expect(validateStoredOutbox(item).ok).toBe(true);
    expect(
      validateStoredOutbox({ ...item, body: { ...item.body, operation_id: "op-2" } }).ok,
    ).toBe(false);
    expect(validateStoredOutbox({ ...item, route: 42 }).ok).toBe(false);
  });
});

describe("replay suspension (N02)", () => {
  it("suspendForReplay stops drains and resumeAfterReplay drops replayed ops", async () => {
    const clock = manualClock();
    const transport = fakeTransport();
    const queue = new SaveQueue(transport, clock);
    // Real order: the controller suspends before queueing anything, then
    // enqueues while the replay is in flight.
    await queue.suspendForReplay();
    queue.enqueue(operation("replayed-op"));
    queue.enqueue(operation("new-op"));
    await queue.drain();
    await flushMicrotasks();
    expect(transport.sent.length).toBe(0);
    queue.resumeAfterReplay(["replayed-op"]);
    queue.continueAfterReplay();
    await flushMicrotasks();
    // Only the not-yet-replayed operation is sent, once, with the writer
    // resumed exactly once.
    expect(transport.sent.map((s) => s.operation_id)).toEqual(["new-op"]);
  });

  it("suspendForReplay cancels a pending backoff timer", async () => {
    const clock = manualClock();
    const transport = fakeTransport({
      onSend: (request) =>
        request.operation_id === "keep" && transport.sent.length <= 1
          ? Promise.reject({ status: 0 })
          : Promise.resolve({ revision: 1 }),
    });
    const queue = new SaveQueue(transport, clock);
    queue.enqueue(operation("keep"));
    queue.start();
    await flushMicrotasks();
    expect(transport.sent.length).toBe(1);
    await queue.suspendForReplay();
    clock.advance(60000);
    await flushMicrotasks();
    // The scheduled retry was cancelled by the suspension.
    expect(transport.sent.length).toBe(1);
    queue.resumeAfterReplay([]);
    queue.continueAfterReplay();
    await flushMicrotasks();
    expect(transport.sent.length).toBe(2);
  });
});

describe("suspendForReplay waits for in-flight writer (T03)", () => {
  it("does not resolve suspension until the in-flight op settles", async () => {
    const clock = manualClock();
    let releaseA!: (value: SaveResult) => void;
    const gateA = new Promise<SaveResult>((resolve) => {
      releaseA = resolve;
    });
    const transport = fakeTransport();
    let gatedId = "";
    transport.send = (async (request: SaveRequestBody) => {
      transport.sent.push(request);
      if (request.operation_id === gatedId) return gateA;
      return { revision: 9 };
    }) as QueueTransport["send"];
    const queue = new SaveQueue(transport, clock);
    queue.noteInput({
      segments: [{ id: 1, text: "A" }],
      review: undefined,
      includeReview: false,
      leaseToken: "lease-1",
      revision: 0,
    });
    const opA = queue.buildNext();
    gatedId = (opA as QueuedOperation).operationId;
    queue.enqueue(opA as QueuedOperation);
    queue.start();
    await flushMicrotasks();
    expect(queue.getSnapshot().inFlightId).toBe(gatedId);

    let suspended = false;
    const suspension = queue.suspendForReplay().then(() => {
      suspended = true;
    });
    await flushMicrotasks();
    // The suspension must NOT complete while A is still in flight.
    expect(suspended).toBe(false);
    releaseA({ revision: 1 });
    await suspension;
    expect(suspended).toBe(true);
    queue.dispose();
  });

  it("a running drain does not send the next queued op once suspended", async () => {
    const clock = manualClock();
    let releaseA!: (value: SaveResult) => void;
    const gateA = new Promise<SaveResult>((resolve) => {
      releaseA = resolve;
    });
    const transport = fakeTransport();
    let gatedId = "";
    transport.send = (async (request: SaveRequestBody) => {
      transport.sent.push(request);
      if (request.operation_id === gatedId) return gateA;
      return { revision: 9 };
    }) as QueueTransport["send"];
    const queue = new SaveQueue(transport, clock);
    queue.noteInput({
      segments: [{ id: 1, text: "A" }],
      review: undefined,
      includeReview: false,
      leaseToken: "lease-1",
      revision: 0,
    });
    const opA = queue.buildNext();
    gatedId = (opA as QueuedOperation).operationId;
    queue.enqueue(opA as QueuedOperation);
    queue.enqueue(operation("op-b"));
    queue.start();
    await flushMicrotasks();
    expect(queue.getSnapshot().inFlightId).toBe(gatedId);

    // Suspend while A is in flight, then release A: the drain must stop and
    // must not shift/send op-b.
    const suspension = queue.suspendForReplay();
    releaseA({ revision: 1 });
    await suspension;
    await flushMicrotasks();
    expect(transport.sent.map((s) => s.operation_id)).toEqual([gatedId]);
    queue.dispose();
  });
});

describe("outbox planning order (X01/X03)", () => {
  it("a corrupt record stops later valid operations from sending", async () => {
    const { replaySaveOutbox } = await import(
      "../features/workspace/persistence/outboxReplay"
    );
    const sent: string[] = [];
    const rows = [
      { operation_id: "bad", username: "u", task_id: "t", route: "/x", method: "PATCH", body: {}, created_at: "2026-01-01" },
      { operation_id: "good", username: "u", task_id: "t", route: "/api/assignment/current", method: "PATCH", created_at: "2026-01-02",
        body: { operation_id: "good", lease_token: "l", expected_revision: 0, segments: [] } },
    ];
    const store = {
      listOutbox: async () => structuredClone(rows),
      confirmOutbox: async (id: string) => {
        const i = rows.findIndex((r) => r.operation_id === id);
        if (i >= 0) rows.splice(i, 1);
      },
      bumpOutboxAttempt: async () => undefined,
    };
    const outcome = await replaySaveOutbox("u", "t", {
      store: store as never,
      send: async (body: { operation_id: string }) => {
        sent.push(body.operation_id);
        return { revision: 1 };
      },
      confirm: async () => undefined,
    });
    expect(sent).toEqual([]);
    expect(outcome.unknownItems.length).toBe(1);
  });

  it("a read error is reported, not treated as an empty queue", async () => {
    const { replaySaveOutbox } = await import(
      "../features/workspace/persistence/outboxReplay"
    );
    const store = {
      listOutbox: async () => {
        throw new DOMException("read failed", "UnknownError");
      },
      confirmOutbox: async () => undefined,
      bumpOutboxAttempt: async () => undefined,
    };
    const outcome = await replaySaveOutbox("u", "t", {
      store: store as never,
      send: async () => ({ revision: 1 }),
      confirm: async () => undefined,
    });
    expect(outcome.readError).toBe(true);
  });
});

describe("stored draft pending_time_edits validation (AB03)", () => {
  it("accepts valid pending time entries and rejects unknown segments", async () => {
    const { validateStoredDraft } = await import(
      "../features/workspace/persistence/offlineAdapter"
    );
    const base = {
      username: "u",
      task_id: "t",
      server_revision: 1,
      lease_token: "l",
      segments: [{ id: 1 }, { id: 2 }],
    };
    expect(
      validateStoredDraft({
        ...base,
        pending_time_edits: [{ segment_id: 1, field: "end", raw: "00:08.250" }],
      }).ok,
    ).toBe(true);
    expect(
      validateStoredDraft({
        ...base,
        pending_time_edits: [{ segment_id: "missing", field: "end", raw: "00:08.250" }],
      }).ok,
    ).toBe(false);
    expect(
      validateStoredDraft({
        ...base,
        pending_time_edits: [
          { segment_id: 1, field: "end", raw: "x" },
          { segment_id: 1, field: "end", raw: "y" },
        ],
      }).ok,
    ).toBe(false);
    expect(
      validateStoredDraft({
        ...base,
        pending_time_edits: "not-an-array",
      }).ok,
    ).toBe(false);
  });
});
