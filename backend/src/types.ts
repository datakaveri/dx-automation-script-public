export type CollectionStatus = "idle" | "running" | "passed" | "failed" | "error";

export interface AssertionResult {
  name: string;
  passed: boolean;
  errorMessage: string | null;
}

export interface ExecutionResult {
  folder: string | null;
  name: string;
  method: string;
  url: string;
  statusCode: number | null;
  responseTimeMs: number | null;
  responseSize: number | null;
  assertions: AssertionResult[];
  requestError: string | null;
  failed: boolean;
  // What the request really sent, with credentials hidden. Absent on runs stored before these were recorded.
  requestHeaders?: Array<{ key: string; value: string }>;
  requestBody?: string | null;
}

export interface SkippedRequest {
  folder: string | null;
  name: string;
  method: string;
  url: string;
}

export interface RunStats {
  requests: { total: number; failed: number };
  assertions: { total: number; failed: number };
  tests: { total: number; failed: number };
  averageResponseTimeMs: number;
}

export interface RunResult {
  startedAt: string;
  completedAt: string;
  durationMs: number;
  stats: RunStats;
  executions: ExecutionResult[];
  // Collection requests that never ran (e.g. jumped over by postman.setNextRequest()).
  skippedRequests?: SkippedRequest[];
}

export interface HistoryRun {
  runId: string;
  startedAt: string;
  completedAt: string;
  durationMs: number;
  status: CollectionStatus;
  stats: RunStats;
  executions: ExecutionResult[];
  skippedRequests?: SkippedRequest[];
}

export interface HistoryRunSummary {
  runId: string;
  runNumber: number;
  startedAt: string;
  completedAt: string;
  durationMs: number;
  status: CollectionStatus;
  totalApis: number;
  assertions: { passed: number; failed: number; total: number };
  avgLatencyMs: number;
  reportUrl: string;
}

export interface CollectionHistoryResponse {
  data: {
    collection: { id: string; folderName: string };
    runs: HistoryRunSummary[];
  };
  meta: { limit: number; count: number };
}

export interface EndpointResult {
  name: string;
  method: string;
  status: "passed" | "failed";
  statusCode: number | null;
  folder: string;
  endpoint: string;
  latencyMs: number | null;
  payloadBytes: number | null;
  assertions: { passed: number; failed: number; total: number };
  verifiedAssertions: AssertionResult[];
}

export interface CollectionLatestRunResponse {
  data: {
    collection: { id: string; folderName: string };
    run: {
      runId: string;
      status: CollectionStatus;
      completedAt: string;
      durationMs: number;
      reportUrl: string;
    };
    kpis: {
      totalEndpoints: number;
      passedRequests: number;
      failedRequests: number;
      avgResponseTimeMs: number;
    };
    folders: Array<{ name: string; endpointCount: number }>;
    endpoints: EndpointResult[];
  };
  meta: {
    filters: { search: string; status: "all" | "passed" | "failed"; method: string; folder: string };
    totalItems: number;
    page: number;
    pageSize: number;
  };
}

export interface CollectionState {
  status: CollectionStatus;
  runId?: string;
  runStartedAt?: string;
  lastRunAt: string | null;
  durationMs: number | null;
  stats: RunStats | null;
  result: RunResult | null;
  history?: HistoryRun[];
}

export interface CollectionDescriptor {
  id: string;
  folderName: string;
  folderPath: string;
  collectionFile: string;
  environmentFile: string;
  postRunFile: string | null;
  fileName: string;
  name: string;
  requestCount: number;
  environment: string | null;
  collection: Record<string, unknown> | null;
  parseError: string | null;
}

export interface PostRunStep {
  name: string;
  command: string;
  args: string[];
  shell?: boolean;
  env?: Record<string, string>;
}

export interface PostRunManifest {
  export?: Record<string, string>;
  ignoreFiles?: string[];
  python?: { requirements: string };
  steps: PostRunStep[];
}

export interface CollectionSummary {
  id: string;
  folderName: string;
  folderPath: string;
  collectionFile: string;
  environmentFile: string;
  fileName: string;
  name: string;
  requestCount: number;
  environment: string | null;
  status: CollectionStatus;
  runId: string | null;
  lastRunAt: string | null;
  durationMs: number | null;
  stats: RunStats | null;
}

export interface SelectedRunQueuedItem {
  collectionId: string;
  runId: string;
  status: "running";
  statusUrl: string;
}

export interface SelectedRunSkippedItem {
  collectionId: string;
  reason: "collection_not_found" | "invalid_collection" | "already_running";
}

export interface SelectedRunOutcome {
  queued: SelectedRunQueuedItem[];
  skipped: SelectedRunSkippedItem[];
}

export interface SkippedFile {
  folderName?: string;
  fileName: string;
  reason: string;
}
