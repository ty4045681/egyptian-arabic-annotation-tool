/**
 * Outbox planning (work package F, workspace/persistence).
 *
 * This module is a PURE validation/sort step: it reads nothing and sends
 * nothing. The single serial writer (`SaveQueue`) owns all network sends and
 * operation-confirmation phases. `planOutbox` receives already-read `unknown`
 * rows and classifies them:
 *
 * - terminal (known `/complete` or `/abandon` routes): retained, handed to the
 *   legacy page; they also BLOCK later normal saves so nothing overtakes them.
 * - save: exact `PATCH /api/assignment/current` with a valid body.
 * - unknown: any other method/route envelope. Retained and blocking; P1 must
 *   never reinterpret an unknown stored request as a current-task PATCH.
 * - corrupt: fails body validation. Retained and blocking.
 *
 * Every list is ordered by `created_at`. The first unsendable record stops the
 * plan: later valid saves may not be sent ahead of it (X01/Y05).
 */

import { classifyStoredOutbox, type OfflineStore } from "./offlineAdapter";
import type { OutboxRecord } from "./types";

export interface PlannedSave {
  item: OutboxRecord;
}

export interface OutboxPlan {
  /** Valid older save operations, in order. The writer sends these first. */
  saves: OutboxRecord[];
  /** Known terminal requests: retain and return to the legacy page. */
  terminal: OutboxRecord[];
  /** Unknown method/route envelopes: retain and block. */
  unknown: unknown[];
  /** Structurally corrupt records: retain and block. */
  corrupt: unknown[];
  /** The outbox read itself failed; NOT an empty queue (X03/Y04). */
  readError: boolean;
}

/** Classify a user's outbox rows for a single task into an ordered plan. */
export function planOutbox(rows: readonly unknown[], taskId: string): OutboxPlan {
  const plan: OutboxPlan = {
    saves: [],
    terminal: [],
    unknown: [],
    corrupt: [],
    readError: false,
  };
  const mine = rows
    .filter(
      (row) =>
        row &&
        typeof row === "object" &&
        String((row as { task_id?: unknown }).task_id) === String(taskId),
    )
    .sort((a, b) =>
      String((a as { created_at?: unknown }).created_at ?? "").localeCompare(
        String((b as { created_at?: unknown }).created_at ?? ""),
      ),
    );
  for (const raw of mine) {
    const kind = classifyStoredOutbox(raw);
    if (kind === "terminal") {
      plan.terminal.push(raw as OutboxRecord);
      break;
    }
    if (kind === "save") {
      plan.saves.push(raw as OutboxRecord);
      continue;
    }
    // Unknown or corrupt: retain and stop; later saves must not overtake it.
    if (kind === "unknown") plan.unknown.push(raw);
    else plan.corrupt.push(raw);
    break;
  }
  return plan;
}

export interface ReplayDeps {
  store: Pick<OfflineStore, "listOutbox" | "bumpOutboxAttempt">;
  /** Send the exact frozen body; resolves with the server revision. */
  send: (body: OutboxRecord["body"]) => Promise<{ revision: number }>;
  /** Persist the confirmation transaction (removes the outbox row). */
  confirm: (item: OutboxRecord, revision: number) => Promise<void>;
}

export interface ReplayOutcome {
  replayed: number;
  terminal: OutboxRecord[];
  failed: OutboxRecord[];
  corrupt: number;
  corruptItems: unknown[];
  unknownItems: unknown[];
  applied: Array<{ item: OutboxRecord; revision: number }>;
  conflicted: OutboxRecord[];
  readError: boolean;
}

/**
 * Read and replay this task's outbox once. Callers must pass a store whose
 * `listOutbox` throws on read failure so a failure is distinguishable from an
 * empty queue (X03/Y04).
 */
export async function replaySaveOutbox(
  username: string,
  taskId: string,
  deps: ReplayDeps,
): Promise<ReplayOutcome> {
  const outcome: ReplayOutcome = {
    replayed: 0,
    terminal: [],
    failed: [],
    corrupt: 0,
    corruptItems: [],
    unknownItems: [],
    applied: [],
    conflicted: [],
    readError: false,
  };
  let rows: unknown[];
  try {
    rows = (await deps.store.listOutbox(username)) as unknown[];
  } catch {
    outcome.readError = true;
    return outcome;
  }
  const plan = planOutbox(rows, taskId);
  outcome.terminal = plan.terminal;
  outcome.corrupt = plan.corrupt.length;
  outcome.corruptItems = plan.corrupt;
  outcome.unknownItems = plan.unknown;
  if (plan.terminal.length > 0 || plan.unknown.length > 0 || plan.corrupt.length > 0) {
    // A blocking record exists: do not send any later save.
    return outcome;
  }
  for (const item of plan.saves) {
    try {
      const result = await deps.send(item.body);
      await deps.confirm(item, result.revision);
      outcome.replayed += 1;
      outcome.applied.push({ item, revision: result.revision });
    } catch (error) {
      const status = Number((error as { status?: unknown })?.status ?? NaN);
      if (status === 409) {
        outcome.conflicted.push(item);
        break;
      }
      try {
        await deps.store.bumpOutboxAttempt(item.operation_id);
      } catch {
        // Attempt bookkeeping must not break the replay loop.
      }
      outcome.failed.push(item);
      break;
    }
  }
  return outcome;
}
