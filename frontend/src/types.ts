export type CollectionStatus = "idle" | "running" | "passed" | "failed" | "error";

export interface RunStats {
  requests: { total: number; failed: number };
  assertions: { total: number; failed: number };
  averageResponseTimeMs: number;
}

export interface Collection {
  id: string;
  folderName: string;
  collectionFile: string;
  environmentFile: string | null;
  status: CollectionStatus;
  runId: string | null;
  apiCount: number;
  assertions: {
    passed: number;
    failed: number;
    total: number;
  };
  tests?: {
    passed: number;
    failed: number;
    total: number;
  };
  lastRunAt: string | null;
  durationMs: number | null;
  actions: {
    canRerun: boolean;
    canViewHistory: boolean;
  };
}

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
}

export interface RunResult {
  startedAt: string;
  completedAt: string;
  durationMs: number;
  stats: RunStats;
  executions: ExecutionResult[];
}

export interface HistoryRunSummary {
  runId: string;
  runNumber: number;
  startedAt: string;
  completedAt: string;
  durationMs: number;
  status: CollectionStatus;
  totalApis: number;
  assertions: {
    passed: number;
    failed: number;
    total: number;
  };
  avgLatencyMs: number;
  reportUrl: string;
}

export interface CollectionHistoryResponse {
  data: {
    collection: { id: string; folderName: string };
    runs: HistoryRunSummary[];
  };
  meta: {
    limit: number;
    count: number;
  };
}

export interface DashboardKpis {
  totalCollections: number;
  passedCollections: number;
  failedCollections: number;
  totalApis: number;
  failedAssertions: number;
}

export interface DashboardResponse {
  data: {
    kpis: DashboardKpis;
    collections: Collection[];
  };
  meta: {
    generatedAt: string;
  };
}

export interface RunStartResponse {
  data: {
    runId: string;
    collectionId: string;
    status: CollectionStatus;
    statusUrl: string;
  };
}

export interface RunStatusResponse {
  data: {
    runId: string;
    collectionId: string;
    status: CollectionStatus;
    startedAt: string | null;
    completedAt: string | null;
    durationMs: number | null;
    stats: RunStats | null;
  };
}

export interface SkippedFile {
  fileName: string;
  reason: string;
}

export type SkippedRunReason = "collection_not_found" | "invalid_collection" | "already_running" | string;

export interface SelectedRunQueuedItem {
  collectionId: string;
  runId: string;
  status: CollectionStatus;
  statusUrl: string;
}

export interface SelectedRunSkippedItem {
  collectionId: string;
  reason: SkippedRunReason;
}

export interface SelectedRunResponse {
  data: {
    queued: SelectedRunQueuedItem[];
    skipped: SelectedRunSkippedItem[];
  };
}

export type AutoRerunInterval = "off" | "15m" | "1h" | "24h";

export interface AutoRerunSettings {
  interval: AutoRerunInterval;
  recipients: string[];
}

export interface AutoRerunSettingsResponse {
  data?: AutoRerunSettings;
  interval?: AutoRerunInterval;
  recipients?: string[];
  message?: string;
  meta?: {
    updatedAt?: string;
    [key: string]: unknown;
  };
}

export interface ApiValidationErrorDetails {
  fieldErrors?: Record<string, string | string[]> | Array<{ field: string; message: string }>;
  [key: string]: unknown;
}

export interface ApiErrorPayload {
  error?: {
    message?: string;
    details?: ApiValidationErrorDetails;
    [key: string]: unknown;
  };
  message?: string;
  details?: ApiValidationErrorDetails;
  [key: string]: unknown;
}

export interface UserRoles {
  realm: string[];
  clients: Record<string, { roles?: string[] }>;
  groups: string[];
}

