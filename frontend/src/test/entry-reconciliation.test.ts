/**
 * Entry-reconciliation matrix for attachAssignment (R03 lost-response
 * reload, R09 stale-clean). Controller-level with an in-memory store:
 * deterministic, no browser needed. Browser tests cover the same flows
 * end to end; these pin the decision table.
 */
import { describe, expect, it } from "vitest";
import {
  PersistenceController,
  type AssignmentContext,
} from "../features/workspace/persistence/persistenceController";

interface MemoryOptions {
  draft?: Record<string, unknown> | null;
  outbox?: Record<string, unknown>[];
}

function memoryStore(options: MemoryOptions = {}) {
  let saved = options.draft ? structuredClone(options.draft) : null;
  let rows = structuredClone(options.outbox ?? []);
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
    remaining: () => structuredClone(rows),
  };
}

const SEGMENT = { id: 1, start: 0, end: 5, duration: 5, text: "server A" };

function context(revision: number): AssignmentContext {
  return {
    username: "matrix-user",
    taskId: "task-1",
    versionId: "version-1",
    leaseToken: "lease-1",
    revision,
    segments: [{ ...SEGMENT }],
    sceneReview: null,
    features: {},
    duration: 10,
  };
}

function draftOf(partial: Record<string, unknown>): Record<string, unknown> {
  return {
    schema_version: 1,
    updated_at: new Date().toISOString(),
    status: "dirty",
    username: "matrix-user",
    task_id: "task-1",
    version_id: "version-1",
    lease_token: "lease-1",
    server_revision: 0,
    segments: [{ ...SEGMENT }],
    scene_review: null,
    dirty_segment_ids: [],
    review_dirty: false,
    mode: "",
    round_id: "",
    ...partial,
  };
}

function saveOp(id: string, revision: number, text: string): Record<string, unknown> {
  return {
    operation_id: id,
    username: "matrix-user",
    task_id: "task-1",
    route: "/api/assignment/current",
    method: "PATCH",
    created_at: new Date().toISOString(),
    attempts: 1,
    body: {
      operation_id: id,
      lease_token: "lease-1",
      expected_revision: revision,
      segments: [{ ...SEGMENT, text }],
    },
  };
}

describe("attachAssignment entry reconciliation", () => {
  it("R03: own accepted-but-unacked op explains the gap (no false conflict)", async () => {
    const store = memoryStore({
      draft: draftOf({
        server_revision: 0,
        segments: [{ ...SEGMENT, text: "newer local B" }],
        dirty_segment_ids: [1],
      }),
      outbox: [saveOp("lost-op", 0, "sent input A")],
    });
    let sends = 0;
    const controller = new PersistenceController({
      store: store as never,
      sender: async () => {
        sends += 1;
        return { revision: 1 };
      },
    });
    const report = await controller.attachAssignment(context(1));
    expect(report.conflict).toBeNull();
    expect(report.replayed).toBe(1);
    expect(sends).toBe(1);
    // Newer local input B survives (only A-covered state is cleared).
    expect(controller.editor.getSegment(1)?.text).toBe("newer local B");
    expect(controller.getSnapshot().revision).toBe(1);
    expect(controller.getSnapshot().conflict).toBeNull();
    expect(store.remaining()).toEqual([]);
    // B is still unsaved work for the next flush (no silent drop).
    expect(controller.needsSave()).toBe(true);
    controller.dispose();
  });

  it("R03: replay rejection (409) is a genuine external conflict", async () => {
    const store = memoryStore({
      draft: draftOf({ server_revision: 0, dirty_segment_ids: [1] }),
      outbox: [saveOp("stale-op", 0, "sent input A")],
    });
    const controller = new PersistenceController({
      store: store as never,
      sender: async () => {
        const error = { status: 409, code: "revision" };
        return Promise.reject(error);
      },
    });
    const report = await controller.attachAssignment(context(1));
    expect(report.conflict?.kind).toBe("revision-mismatch");
    expect(controller.getSnapshot().frozenReason).toBe("revision-conflict");
    // Local work is preserved, nothing is overwritten or dropped.
    expect(controller.editor.getSegment(1)?.text).toBe("server A");
    expect(store.remaining().length).toBe(1);
    controller.dispose();
  });

  it("genuine external gap with no own ops still freezes", async () => {
    const store = memoryStore({
      draft: draftOf({
        server_revision: 0,
        segments: [{ ...SEGMENT, text: "local edit" }],
        dirty_segment_ids: [1],
      }),
      outbox: [],
    });
    let sends = 0;
    const controller = new PersistenceController({
      store: store as never,
      sender: async () => {
        sends += 1;
        return { revision: 1 };
      },
    });
    const report = await controller.attachAssignment(context(1));
    expect(report.conflict?.kind).toBe("revision-mismatch");
    expect(controller.getSnapshot().frozenReason).toBe("revision-conflict");
    expect(sends).toBe(0);
    controller.dispose();
  });

  it("R09: stale clean cache never rewinds newer server state", async () => {
    const store = memoryStore({
      draft: draftOf({
        status: "clean",
        server_revision: 0,
        segments: [{ ...SEGMENT, text: "old clean content" }],
        dirty_segment_ids: [],
        review_dirty: false,
      }),
      outbox: [],
    });
    let sends = 0;
    const controller = new PersistenceController({
      store: store as never,
      sender: async () => {
        sends += 1;
        return { revision: 1 };
      },
    });
    const report = await controller.attachAssignment(context(1));
    expect(report.conflict).toBeNull();
    expect(controller.editor.getSegment(1)?.text).toBe("server A");
    expect(controller.getSnapshot().revision).toBe(1);
    expect(sends).toBe(0);
    expect(controller.needsSave()).toBe(false);
    controller.dispose();
  });

  it("same-revision clean cache restores quietly", async () => {
    const store = memoryStore({
      draft: draftOf({
        status: "clean",
        server_revision: 1,
        segments: [{ ...SEGMENT, text: "server A" }],
        dirty_segment_ids: [],
        review_dirty: false,
      }),
      outbox: [],
    });
    const controller = new PersistenceController({
      store: store as never,
      sender: async () => ({ revision: 1 }),
    });
    const report = await controller.attachAssignment(context(1));
    expect(report.conflict).toBeNull();
    expect(controller.editor.getSegment(1)?.text).toBe("server A");
    controller.dispose();
  });
});

describe("conflict & recovery write gating (T01/T02)", () => {
  function memoryDraftStore() {
    let saved: Record<string, unknown> | null = null;
    return {
      saved: () => saved,
      store: {
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
        putOutbox: async (x: Record<string, unknown>) => x,
        getOutboxItem: async () => null,
        listOutbox: async () => [],
        readOutbox: async () => ({ status: "ok", value: [] }),
        bumpOutboxAttempt: async () => undefined,
        confirmOutbox: async () => undefined,
        purgeExpired: async () => undefined,
    deleteTaskData: async () => {
          saved = null;
        },
        exportText: (x: unknown) => JSON.stringify(x),
      },
    };
  }

  it("T01: an in-flight 409 still persists the newest editor input", async () => {
    const mem = memoryDraftStore();
    let rejectSave!: (error: unknown) => void;
    const gate = new Promise<never>((_resolve, reject) => {
      rejectSave = reject;
    });
    const controller = new PersistenceController({
      store: mem.store as never,
      sender: () => gate,
    });
    await controller.attachAssignment(context(0));
    // Type A, trigger a manual save (in flight), then type B before the
    // 409 response arrives.
    controller.updateSegment(1, { text: "sent A" });
    void controller.flush({ manual: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    controller.updateSegment(1, { text: "latest B" });
    rejectSave({ status: 409, body: { current_revision: 1 } });
    await new Promise((resolve) => setTimeout(resolve, 60));

    expect(controller.getSnapshot().conflict).not.toBeNull();
    const draft = mem.saved() as { segments?: Array<{ text: string }>; status?: string } | null;
    expect(draft?.segments?.[0]?.text).toBe("latest B");
    expect(controller.exportLocalText()).toContain("latest B");
    controller.dispose();
  });

  it("T02: recoveryPending blocks flush/update until re-attach", async () => {
    const mem = memoryDraftStore();
    let sends = 0;
    const controller = new PersistenceController({
      store: mem.store as never,
      sender: async () => {
        sends += 1;
        return { revision: 1 };
      },
    });
    await controller.attachAssignment(context(0));
    await controller.discardLocalConflict();
    expect(controller.getSnapshot().recoveryPending).toBe(true);

    controller.updateSegment(1, { text: "should not enter the editor" });
    await controller.flush({ manual: true });
    await controller.handleOnline();
    expect(sends).toBe(0);
    expect(controller.editor.getSegment(1)?.text).toBe("server A");

    // Re-attach clears the gate and edits flow again.
    await controller.attachAssignment(context(0), { force: true });
    expect(controller.getSnapshot().recoveryPending).toBe(false);
    controller.dispose();
  });
});

describe("concurrent replay serialization (U02)", () => {
  function memStore() {
    let rows: Record<string, unknown>[] = [];
    let saved: Record<string, unknown> | null = null;
    return {
      seedOutbox: (item: Record<string, unknown>) => {
        rows.push(structuredClone(item));
      },
      store: {
        storageHealth: "ok" as const,
        draftKey: (u: string, t: string) => `${u}:${t}`,
        saveWorkingDraft: async (x: Record<string, unknown>) => {
          saved = structuredClone(x);
          return saved;
        },
        getWorkingDraft: async () => structuredClone(saved),
        readWorkingDraft: async () => ({ status: "ok", value: structuredClone(saved) }),
        listWorkingDrafts: async () => [],
        deleteWorkingDraft: async () => undefined,
        putOutbox: async (x: Record<string, unknown>) => {
          rows.push(structuredClone(x));
          return x;
        },
        getOutboxItem: async () => null,
        listOutbox: async () => structuredClone(rows),
        readOutbox: async () => ({ status: "ok", value: structuredClone(rows) }),
        bumpOutboxAttempt: async () => undefined,
        confirmOutbox: async (id: string) => {
          rows = rows.filter((r) => r.operation_id !== id);
        },
        purgeExpired: async () => undefined,
    deleteTaskData: async () => {
          rows = [];
        },
        exportText: (x: unknown) => JSON.stringify(x),
      },
    };
  }

  it("two concurrent handleOnline calls send the persisted op exactly once", async () => {
    const mem = memStore();
    const op = {
      operation_id: "op-A",
      username: "matrix-user",
      task_id: "task-1",
      route: "/api/assignment/current",
      method: "PATCH",
      created_at: new Date().toISOString(),
      attempts: 0,
      body: {
        operation_id: "op-A",
        lease_token: "lease-1",
        expected_revision: 0,
        segments: [{ ...SEGMENT, text: "replayed A" }],
      },
    };
    let sends = 0;
    const controller = new PersistenceController({
      store: mem.store as never,
      sender: async () => {
        sends += 1;
        // Hold the replay open so the second online handler races it.
        await new Promise((resolve) => setTimeout(resolve, 30));
        return { revision: 1 };
      },
    });
    await controller.attachAssignment(context(0));
    // Attach consumed the (empty) outbox; seed one and reset for the race.
    mem.seedOutbox(op);
    sends = 0;
    await Promise.all([controller.handleOnline(), controller.handleOnline()]);
    await new Promise((resolve) => setTimeout(resolve, 20));
    // The mutex means the second caller shares the first replay: exactly one
    // send, and the revision is not rewound.
    expect(sends).toBe(1);
    expect(controller.getSnapshot().revision).toBe(1);
    expect(controller.getSnapshot().conflict).toBeNull();
    controller.dispose();
  });
});

describe("export includes newest in-memory input (U04)", () => {
  it("exports the editor snapshot even when the stored draft is older", async () => {
    let saved: Record<string, unknown> | null = null;
    const store = {
      storageHealth: "quota-exceeded" as const,
      draftKey: (u: string, t: string) => `${u}:${t}`,
      saveWorkingDraft: async (x: Record<string, unknown>) => {
        // Simulate a quota failure after the first successful write.
        if (saved) throw new DOMException("Quota exceeded", "QuotaExceededError");
        saved = structuredClone(x);
        return saved;
      },
      getWorkingDraft: async () => structuredClone(saved),
      readWorkingDraft: async () => ({ status: "ok", value: structuredClone(saved) }),
      listWorkingDrafts: async () => [],
      deleteWorkingDraft: async () => undefined,
      putOutbox: async (x: Record<string, unknown>) => x,
      getOutboxItem: async () => null,
      listOutbox: async () => [],
      readOutbox: async () => ({ status: "ok", value: [] }),
      bumpOutboxAttempt: async () => undefined,
      confirmOutbox: async () => undefined,
      purgeExpired: async () => undefined,
      deleteTaskData: async () => undefined,
      exportText: (x: unknown) => JSON.stringify(x),
    };
    const controller = new PersistenceController({
      store: store as never,
      sender: async () => ({ revision: 1 }),
    });
    await controller.attachAssignment(context(0));
    // First save persists "older draft A" locally.
    controller.updateSegment(1, { text: "older draft A" });
    await controller.flush({ manual: true });
    await new Promise((resolve) => setTimeout(resolve, 20));
    // Now storage fails; newer input stays only in memory.
    controller.updateSegment(1, { text: "LATEST UNSAVED B" });
    await new Promise((resolve) => setTimeout(resolve, 30));
    const exported = controller.exportLocalText();
    expect(exported).toContain("LATEST UNSAVED B");
    controller.dispose();
  });
});

describe("storage barrier & retention (batch 1)", () => {
  function barrierStore(overrides: Record<string, unknown> = {}) {
    return {
      storageHealth: "ok" as const,
      draftKey: (u: string, t: string) => `${u}:${t}`,
      saveWorkingDraft: async (x: Record<string, unknown>) => x,
      getWorkingDraft: async () => null,
      readWorkingDraft: async () => ({ status: "ok" as const, value: null }),
      listWorkingDrafts: async () => [],
      deleteWorkingDraft: async () => undefined,
      putOutbox: async (x: Record<string, unknown>) => x,
      getOutboxItem: async () => null,
      listOutbox: async () => [],
      readOutbox: async () => ({ status: "ok" as const, value: [] }),
      bumpOutboxAttempt: async () => undefined,
      confirmOutbox: async () => undefined,
      deleteTaskData: async () => undefined,
      purgeExpired: async () => undefined,
      exportText: (x: unknown) => JSON.stringify(x),
      ...overrides,
    };
  }

  it("W08: a failing draft read blocks writes and reports a barrier", async () => {
    let saves = 0;
    const store = barrierStore({
      readWorkingDraft: async () => ({
        status: "error" as const,
        health: "unavailable" as const,
        error: "idb gone",
      }),
      saveWorkingDraft: async (x: Record<string, unknown>) => {
        saves += 1;
        return x;
      },
    });
    const controller = new PersistenceController({
      store: store as never,
      sender: async () => ({ revision: 1 }),
    });
    await controller.attachAssignment(context(0));
    expect(controller.getSnapshot().storageBarrier).toBe("draft-read-error");
    controller.updateSegment(1, { text: "should not send" });
    await controller.flush({ manual: true });
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(saves).toBe(0);
    controller.dispose();
  });

  it("V03: a corrupt draft blocks overwrite and keeps the error visible", async () => {
    let saves = 0;
    const store = barrierStore({
      readWorkingDraft: async () => ({
        status: "ok" as const,
        value: { username: "matrix-user", task_id: "task-1", segments: "not-an-array" },
      }),
      saveWorkingDraft: async (x: Record<string, unknown>) => {
        saves += 1;
        return x;
      },
    });
    const controller = new PersistenceController({
      store: store as never,
      sender: async () => ({ revision: 1 }),
    });
    await controller.attachAssignment(context(0));
    expect(controller.getSnapshot().storageBarrier).toBe("corrupt-draft");
    controller.updateSegment(1, { text: "would overwrite" });
    await new Promise((resolve) => setTimeout(resolve, 60));
    // Editing is allowed in memory for export, but nothing is written.
    expect(saves).toBe(0);
    expect(controller.getSnapshot().error).toContain("damaged");
    controller.dispose();
  });

  it("V04: a corrupt outbox blocks new PATCHes", async () => {
    let sends = 0;
    const corrupt = [
      { operation_id: "op-bad", task_id: "task-1", route: "/x", method: "PATCH", body: {} },
    ];
    const store = barrierStore({
      listOutbox: async () => structuredClone(corrupt),
      readOutbox: async () => ({ status: "ok" as const, value: structuredClone(corrupt) }),
    });
    const controller = new PersistenceController({
      store: store as never,
      sender: async () => {
        sends += 1;
        return { revision: 1 };
      },
    });
    await controller.attachAssignment(context(0));
    expect(controller.getSnapshot().storageBarrier).toBe("corrupt-outbox");
    controller.updateSegment(1, { text: "new save" });
    await controller.flush({ manual: true });
    await new Promise((resolve) => setTimeout(resolve, 10));
    expect(sends).toBe(0);
    controller.dispose();
  });

  it("V01: export strips blind metadata recursively from nested drafts", async () => {
    let saved: Record<string, unknown> | null = null;
    const store = barrierStore({
      saveWorkingDraft: async (x: Record<string, unknown>) => {
        saved = structuredClone(x);
        return saved;
      },
      readWorkingDraft: async () => ({ status: "ok" as const, value: null }),
    });
    const controller = new PersistenceController({
      store: store as never,
      sender: async () => ({ revision: 1 }),
    });
    const ctx = { ...context(0), mode: "cross_check", roundId: "HIDDEN-ROUND" };
    await controller.attachAssignment(ctx);
    const exported = controller.exportLocalText();
    expect(exported).not.toContain("cross_check");
    expect(exported).not.toContain("HIDDEN-ROUND");
    expect(exported).not.toContain("round_id");
    controller.dispose();
  });

  it("W05: review writes are rejected when the feature flags are off", async () => {
    const controller = new PersistenceController({
      store: barrierStore() as never,
      sender: async () => ({ revision: 1 }),
    });
    const ctx = {
      ...context(0),
      features: { metadata_ui: false, scene_review_write: false },
    };
    await controller.attachAssignment(ctx);
    controller.updateReview({ status: "confirmed", scene_codes: ["airport"], note: "x" });
    expect(controller.getSnapshot().reviewDirty).toBe(false);
    expect(controller.needsSave()).toBe(false);
    controller.dispose();
  });
});

describe("pending time draft persistence (AA03/AA04)", () => {
  function timeStore() {
    let saved: Record<string, unknown> | null = null;
    let rows: Record<string, unknown>[] = [];
    return {
      saved: () => saved,
      store: {
        storageHealth: "ok" as const,
        draftKey: (u: string, t: string) => `${u}:${t}`,
        saveWorkingDraft: async (x: Record<string, unknown>) => {
          saved = structuredClone(x);
          return saved;
        },
        getWorkingDraft: async () => structuredClone(saved),
        readWorkingDraft: async () => ({ status: "ok" as const, value: structuredClone(saved) }),
        listWorkingDrafts: async () => [],
        deleteWorkingDraft: async () => undefined,
        putOutbox: async (x: Record<string, unknown>) => x,
        getOutboxItem: async () => null,
        listOutbox: async () => structuredClone(rows),
        readOutbox: async () => ({ status: "ok" as const, value: structuredClone(rows) }),
        bumpOutboxAttempt: async () => undefined,
        confirmOutbox: async () => undefined,
        deleteTaskData: async () => {
          rows = [];
          saved = null;
        },
        purgeExpired: async () => undefined,
        exportText: (x: unknown) => JSON.stringify(x),
      },
    };
  }

  it("persists pending time raw text and status dirty (AA03)", async () => {
    const mem = timeStore();
    const controller = new PersistenceController({
      store: mem.store as never,
      sender: async () => ({ revision: 1 }),
      persistDelayMs: 5,
    });
    await controller.attachAssignment(context(0));
    controller.editor.setPendingTime(1, "end", "00:08.250");
    await new Promise((resolve) => setTimeout(resolve, 30));
    const draft = mem.saved() as {
      pending_time_edits?: Array<{ raw: string; field: string }>;
      status?: string;
    } | null;
    expect(draft?.pending_time_edits?.[0]?.raw).toBe("00:08.250");
    expect(draft?.status).toBe("dirty");
    // Export reflects the same unsaved definition (AA04).
    const exported = JSON.parse(controller.exportLocalText()) as {
      status: string;
      pending_time_edits: Array<{ raw: string }>;
    };
    expect(exported.status).toBe("dirty");
    expect(exported.pending_time_edits[0]?.raw).toBe("00:08.250");
    controller.dispose();
  });

  it("restores pending time raw text on re-attach (AA03)", async () => {
    const mem = timeStore();
    const first = new PersistenceController({
      store: mem.store as never,
      sender: async () => ({ revision: 1 }),
      persistDelayMs: 5,
    });
    await first.attachAssignment(context(0));
    first.editor.setPendingTime(1, "end", "00:03.500");
    await new Promise((resolve) => setTimeout(resolve, 30));
    // Simulate a reload: a fresh controller over the same store.
    const second = new PersistenceController({
      store: mem.store as never,
      sender: async () => ({ revision: 1 }),
    });
    await second.attachAssignment(context(0));
    expect(second.editor.pendingTime(1, "end")).toBe("00:03.500");
    expect(second.getSnapshot().pendingTimeCount).toBe(1);
    second.dispose();
    first.dispose();
  });
});
