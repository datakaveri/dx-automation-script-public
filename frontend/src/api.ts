import type {
  ApiErrorPayload,
  ApiValidationErrorDetails,
  AutoRerunSettings,
  AutoRerunSettingsResponse,
  CollectionHistoryResponse,
  DashboardResponse,
  RunResult,
  RunStartResponse,
  RunStatusResponse,
  SelectedRunResponse,
  SkippedFile,
} from "./types";
import { keycloak, keycloakReady } from "./keycloak";

const envApiUrl =
  typeof import.meta.env.VITE_API_BASE_URL === "string"
    ? import.meta.env.VITE_API_BASE_URL.trim()
    : "";
const API_BASE_URL = (
  envApiUrl.length > 0
    ? envApiUrl
    : import.meta.env.DEV
      ? "http://localhost:4000"
      : "https://iudxqa-backend.slicearrow.com"
).replace(/\/$/, "");

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

export class ApiError extends Error {
  status: number;
  details?: ApiValidationErrorDetails;
  raw?: unknown;

  constructor(
    message: string,
    status: number,
    details?: ApiValidationErrorDetails,
    raw?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
    this.raw = raw;
  }
}

export function extractFieldErrors(error: unknown): Record<string, string[]> {
  const result: Record<string, string[]> = {};
  if (!error) return result;

  let fieldErrors: unknown = undefined;
  if (error instanceof ApiError && error.details?.fieldErrors) {
    fieldErrors = error.details.fieldErrors;
  } else if (typeof error === "object" && error !== null) {
    const errObj = error as {
      details?: { fieldErrors?: unknown };
      error?: { details?: { fieldErrors?: unknown } };
    };
    fieldErrors =
      errObj.details?.fieldErrors ?? errObj.error?.details?.fieldErrors;
  }

  if (!fieldErrors) return result;

  if (Array.isArray(fieldErrors)) {
    for (const item of fieldErrors) {
      if (item && typeof item === "object" && "field" in item && "message" in item) {
        const field = String((item as { field: string }).field);
        const msg = String((item as { message: string }).message);
        if (!result[field]) result[field] = [];
        result[field].push(msg);
      }
    }
  } else if (typeof fieldErrors === "object") {
    for (const [key, value] of Object.entries(fieldErrors as Record<string, unknown>)) {
      if (Array.isArray(value)) {
        result[key] = value.map((v) => String(v));
      } else if (typeof value === "string") {
        result[key] = [value];
      }
    }
  }

  return result;
}

async function request<T>(
  url: string,
  init?: RequestInit,
  retry = true,
): Promise<T> {
  await keycloakReady();
  if (!keycloak?.authenticated)
    throw new Error("Your Keycloak session has ended. Please sign in again.");
  await keycloak.updateToken(30);
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${keycloak.token}`);
  const response = await fetch(apiUrl(url), { ...init, headers });
  if (!response.ok) {
    if (response.status === 401 && retry) {
      await keycloak.updateToken(-1);
      return request<T>(url, init, false);
    }
    const body = (await response.json().catch(() => null)) as ApiErrorPayload | null;
    const message =
      body?.error?.message ??
      body?.message ??
      `Request failed with status ${response.status}.`;
    const details = body?.error?.details ?? body?.details;
    throw new ApiError(message, response.status, details, body);
  }
  return response.json() as Promise<T>;
}

export async function openAuthenticatedReport(
  reportPath: string,
  reportTab: Window | null,
): Promise<void> {
  await keycloakReady();
  if (!keycloak?.authenticated)
    throw new Error("Please sign in to view this report.");
  if (!reportTab) throw new Error("Allow pop-ups to open the report.");
  await keycloak.updateToken(30);
  const reportUrl = new URL(reportPath, `${API_BASE_URL}/`);
  if (reportUrl.origin !== new URL(API_BASE_URL).origin)
    throw new Error("The report URL is outside the configured API origin.");
  const response = await fetch(reportUrl, {
    headers: { Authorization: `Bearer ${keycloak.token}` },
  });
  if (!response.ok)
    throw new Error(
      response.status === 401
        ? "Your session is no longer valid."
        : "Could not load this report.",
    );
  const objectUrl = URL.createObjectURL(await response.blob());
  reportTab.location.href = objectUrl;
}

export const api = {
  me: () =>
    request<{
      user: {
        id: string;
        username: string | null;
        email: string | null;
        name: string | null;
        roles: {
          realm: string[];
          clients: Record<string, { roles?: string[] }>;
          groups: string[];
        };
      };
    }>("/api/auth/me"),
  dashboard: () => request<DashboardResponse>("/api/dashboard"),
  skipped: () => request<SkippedFile[]>("/api/skipped"),
  result: (id: string) =>
    request<{ result: RunResult | null }>(
      `/api/collections/${encodeURIComponent(id)}/result`,
    ),
  history: (id: string) =>
    request<CollectionHistoryResponse>(
      `/api/collections/${encodeURIComponent(id)}/history`,
    ),
  run: (id: string) =>
    request<RunStartResponse>(
      `/api/collections/${encodeURIComponent(id)}/run`,
      { method: "POST" },
    ),
  runStatus: (runId: string) =>
    request<RunStatusResponse>(`/api/runs/${encodeURIComponent(runId)}`),
  runSelected: (collectionIds: string[]) =>
    request<SelectedRunResponse>("/api/run-selected", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ collectionIds }),
    }),
  runAll: () => request("/api/run-all", { method: "POST" }),
  getAutoRerunSettings: () =>
    request<AutoRerunSettingsResponse>("/api/settings/auto-rerun"),
  saveAutoRerunSettings: (settings: AutoRerunSettings) =>
    request<AutoRerunSettingsResponse>("/api/settings/auto-rerun", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    }),
};

