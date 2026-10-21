import type { CollectionState, CollectionStatus, HistoryRun, RunResult } from "./types";

export interface ResultsStorePort {
  load(): Promise<void>;
  reload(): Promise<void>;
  get(id: string): CollectionState;
  markRunning(id: string): Promise<string>;
  complete(id: string, status: Exclude<CollectionStatus, "idle" | "running">, result: RunResult | null, customHistory?: HistoryRun[]): Promise<void>;
  findRun(runId: string): { collectionId: string; state: CollectionState; run?: HistoryRun } | null;
}
