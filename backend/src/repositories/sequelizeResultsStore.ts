import type { ModelStatic } from "sequelize";
import { randomUUID } from "node:crypto";
import type { CollectionState, CollectionStatus, HistoryRun, RunResult, RunStats } from "../types";
import type { ResultsStorePort } from "../store";
import { CollectionStateModel } from "../database/models/collectionState";

const emptyStats = (): RunStats => ({
  requests: { total: 0, failed: 0 },
  assertions: { total: 0, failed: 0 },
  tests: { total: 0, failed: 0 },
  averageResponseTimeMs: 0
});

const idleState = (): CollectionState => ({
  status: "idle",
  lastRunAt: null,
  durationMs: null,
  stats: null,
  result: null
});

export class SequelizeResultsStore implements ResultsStorePort {
  private collections: Record<string, CollectionState> = {};
  private writes = Promise.resolve();

  constructor(private readonly model: ModelStatic<CollectionStateModel>) {}

  async load(): Promise<void> {
    const rows = await this.model.findAll();
    this.collections = Object.fromEntries(rows.map((row) => [row.collectionId, row.state]));
  }

  get(id: string): CollectionState {
    const state = this.collections[id] ?? idleState();
    if (state.status === "idle" && state.result && (state.stats?.requests.failed ?? 0) === 0) {
      return { ...state, status: "passed" };
    }
    return state;
  }

  async reload(): Promise<void> {
    await this.load();
  }

  async markRunning(id: string): Promise<string> {
    const current = this.get(id);
    const runId = `run-${randomUUID()}`;
    this.collections[id] = { ...current, status: "running", runId, runStartedAt: new Date().toISOString() };
    await this.save(id);
    return runId;
  }

  async complete(id: string, status: Exclude<CollectionStatus, "idle" | "running">, result: RunResult | null, customHistory?: HistoryRun[]): Promise<void> {
    const stats = result?.stats ?? emptyStats();
    const current = this.get(id);
    const existingHistory = customHistory ?? current.history ?? [];

    let updatedHistory = existingHistory;
    if (result && !customHistory) {
      const newEntry: HistoryRun = {
        runId: current.runId ?? `run-${randomUUID()}`,
        startedAt: result.startedAt,
        completedAt: result.completedAt,
        durationMs: result.durationMs,
        status,
        stats,
        executions: result.executions,
        skippedRequests: result.skippedRequests
      };
      updatedHistory = [newEntry, ...existingHistory].slice(0, 5);
    }

    this.collections[id] = {
      status,
      runId: current.runId,
      runStartedAt: result?.startedAt ?? current.runStartedAt,
      lastRunAt: result?.completedAt ?? new Date().toISOString(),
      durationMs: result?.durationMs ?? null,
      stats,
      result,
      history: updatedHistory
    };
    await this.save(id);
  }

  findRun(runId: string): { collectionId: string; state: CollectionState; run?: HistoryRun } | null {
    for (const [collectionId, state] of Object.entries(this.collections)) {
      const run = state.history?.find((historyRun) => historyRun.runId === runId);
      if (run || state.runId === runId) return { collectionId, state, run };
    }
    return null;
  }

  private async save(collectionId: string): Promise<void> {
    this.writes = this.writes.then(async () => {
      await this.model.upsert({ collectionId, state: this.collections[collectionId] });
    });
    await this.writes;
  }
}
