/**
 * Mount/unmount lifecycle invariance (review round-2): the persistence
 * controller holds a single heartbeat and one save queue per instance, and
 * dispose() tears every subscription/timer down so a React remount cannot
 * leave a leaked heartbeat/listener behind. These are synchronous unit
 * assertions on the real controller with an in-memory store — the browser
 * reload test cannot detect same-document leaks.
 */
import { describe, expect, it } from "vitest";
import { PersistenceController } from "../features/workspace/persistence/persistenceController";
import type { AssignmentContext } from "../features/workspace/persistence/persistenceController";
import { SessionService } from "../features/auth/sessionService";

function memoryStore() {
  let saved: Record<string, unknown> | null = null;
  let rows: Record<string, unknown>[] = [];
  return {
    storageHealth: "ok" as const,
    draftKey: (u: string, t: string) => `${u}:${t}`,
    saveWorkingDraft: async (x: Record<string, unknown>) => {
      saved = structuredClone(x);
      return saved;
    },
    getWorkingDraft: async () => structuredClone(saved),
    readWorkingDraft: async () => ({ status: "ok", value: structuredClone(saved) }),
    listWorkingDrafts: async () => (saved ? [structuredClone(saved)] : []),
    deleteWorkingDraft: async () => {
      saved = null;
    },
    putOutbox: async (x: Record<string, unknown>) => {
      rows.push(structuredClone(x));
      return x;
    },
    getOutboxItem: async (id: string) => structuredClone(rows.find((r) => r.operation_id === id)) ?? null,
    listOutbox: async () => structuredClone(rows),
    readOutbox: async () => ({ status: "ok", value: structuredClone(rows) }),
    bumpOutboxAttempt: async () => undefined,
    confirmOutbox: async (id: string) => {
      rows = rows.filter((r) => r.operation_id !== id);
    },
    purgeExpired: async () => undefined,
    deleteTaskData: async () => {
      saved = null;
      rows = [];
    },
    exportText: (x: unknown) => JSON.stringify(x),
  };
}

const SEGMENT = { id: 1, start: 0, end: 5, duration: 5, text: "hello" };

function context(): AssignmentContext {
  return {
    username: "lifecycle-user",
    taskId: "task-1",
    versionId: "version-1",
    leaseToken: "lease-1",
    revision: 0,
    segments: [{ ...SEGMENT }],
    sceneReview: null,
    features: {},
    duration: 10,
  };
}

describe("controller mount/unmount lifecycle", () => {
  it("keeps exactly one heartbeat timer and clears it on dispose", async () => {
    const sessions: SessionService[] = [];
    const controller = new PersistenceController({
      store: memoryStore() as never,
      sender: async () => ({ revision: 1 }),
      sessionFactory: (hooks) => {
        const service = new SessionService(
          { heartbeatSeconds: 30 },
          {
            persist: hooks.persist,
            hasDirty: hooks.hasDirty,
            onFrozen: hooks.onFrozen,
            onOnline: hooks.onOnline,
          },
        );
        sessions.push(service);
        return service;
      },
    });
    await controller.attachAssignment(context());
    controller.attachSession({ heartbeatSeconds: 30 }, "lifecycle-user");
    controller.start();
    controller.start();
    expect(sessions.length).toBe(1);
    expect(sessions[0]?.hasTimer()).toBe(true);

    controller.dispose();
    expect(sessions[0]?.hasTimer()).toBe(false);
    // A disposed controller refuses to schedule again (no resurrected timer).
    controller.start();
    expect(sessions[0]?.hasTimer()).toBe(false);
  });

  it("dispose removes the queue subscription and blocks further sends", async () => {
    let sends = 0;
    const controller = new PersistenceController({
      store: memoryStore() as never,
      sender: async () => {
        sends += 1;
        return { revision: 1 };
      },
    });
    await controller.attachAssignment(context());
    controller.start();
    // Observer count is opaque, but a disposed controller must not send.
    controller.dispose();
    controller.updateSegment(1, { text: "after dispose" });
    await controller.flush({ manual: true });
    expect(sends).toBe(0);
  });

  it("a fresh controller instance starts with a clean single queue", async () => {
    // Simulates React unmount -> remount: the old instance is disposed, the
    // new one owns its own queue and does not replay the other's work.
    const first = new PersistenceController({
      store: memoryStore() as never,
      sender: async () => ({ revision: 1 }),
    });
    await first.attachAssignment(context());
    first.dispose();

    let sends = 0;
    const second = new PersistenceController({
      store: memoryStore() as never,
      sender: async () => {
        sends += 1;
        return { revision: 1 };
      },
    });
    await second.attachAssignment(context());
    second.start();
    second.updateSegment(1, { text: "remounted edit" });
    await second.flush({ manual: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(sends).toBe(1);
    second.dispose();
  });
});
