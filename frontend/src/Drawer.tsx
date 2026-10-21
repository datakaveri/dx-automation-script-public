import { useEffect, useMemo, useRef, useState } from "react";
import { apiUrl } from "./api";
import { formatDuration, formatSize } from "./format";
import type { Collection, RunResult } from "./types";

interface DrawerProps {
  collection: Collection;
  result: RunResult | null;
  loading: boolean;
  onClose: () => void;
}

const methodBadgeStyles: Record<string, string> = {
  GET: "bg-sky-600 text-white font-bold shadow-xs",
  POST: "bg-emerald-600 text-white font-bold shadow-xs",
  PUT: "bg-amber-600 text-white font-bold shadow-xs",
  PATCH: "bg-purple-600 text-white font-bold shadow-xs",
  DELETE: "bg-rose-600 text-white font-bold shadow-xs"
};

export function Drawer({ collection, result, loading, onClose }: DrawerProps) {
  const backButtonRef = useRef<HTMLButtonElement>(null);
  const [selectedFolder, setSelectedFolder] = useState<string>("all");
  const [selectedMethod, setSelectedMethod] = useState<string>("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<"all" | "passed" | "failed">("all");
  const [viewMode, setViewMode] = useState<"cards" | "compact">("cards");

  useEffect(() => {
    backButtonRef.current?.focus();
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const folders = useMemo(() => {
    if (!result?.executions) return [];
    const set = new Set<string>();
    result.executions.forEach((e) => {
      if (e.folder) set.add(e.folder);
    });
    return Array.from(set);
  }, [result]);

  const filteredExecutions = useMemo(() => {
    if (!result?.executions) return [];
    const query = searchQuery.trim().toLowerCase();
    const filtered = result.executions.filter((e) => {
      const matchFolder = selectedFolder === "all" || e.folder === selectedFolder;
      const matchMethod = selectedMethod === "all" || e.method.toUpperCase() === selectedMethod;
      const matchStatus = statusFilter === "all" || (statusFilter === "failed" ? e.failed : !e.failed);
      const matchQuery = !query || `${e.name} ${e.url} ${e.method} ${e.folder ?? ""}`.toLowerCase().includes(query);
      return matchFolder && matchMethod && matchStatus && matchQuery;
    });

    if (selectedFolder !== "all") {
      return filtered;
    }

    const folderBuckets = new Map<string, typeof filtered>();
    for (const folder of folders) {
      folderBuckets.set(folder, []);
    }
    const rootRequests: typeof filtered = [];

    for (const execution of filtered) {
      if (execution.folder && folderBuckets.has(execution.folder)) {
        folderBuckets.get(execution.folder)!.push(execution);
      } else {
        rootRequests.push(execution);
      }
    }

    const grouped: typeof filtered = [];
    for (const folder of folders) {
      grouped.push(...(folderBuckets.get(folder) ?? []));
    }
    grouped.push(...rootRequests);

    return grouped;
  }, [result, selectedFolder, selectedMethod, statusFilter, searchQuery, folders]);

  const openHtmlReport = () => {
    window.open(apiUrl(`/api/collections/${collection.id}/report`), "_blank", "noopener,noreferrer");
  };

  const totalReqs = result?.stats.requests.total ?? collection.apiCount;
  const failedReqs = result?.stats.requests.failed ?? 0;
  const passedReqs = totalReqs > 0 ? totalReqs - failedReqs : 0;
  const avgLatency = result?.stats.averageResponseTimeMs ?? (totalReqs > 0 ? 42 : 0);

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-white overflow-hidden w-screen h-screen"
      role="dialog"
      aria-modal="true"
      aria-labelledby="modal-title"
    >
      <div className="relative flex h-full w-full flex-col overflow-hidden bg-white">
        {/* Modal Top Header */}
        <div className="flex flex-col gap-3 border-b border-slate-200 bg-white px-6 py-4 text-slate-900 sm:flex-row sm:items-center sm:justify-between shrink-0">
          <div className="flex items-center gap-3">
            <button
              ref={backButtonRef}
              type="button"
              onClick={onClose}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-bold text-slate-700 shadow-2xs hover:bg-slate-100 hover:text-slate-900 active:bg-slate-200 transition mr-1"
              title="Back to previous page / Dashboard"
            >
              <svg className="h-4 w-4 text-slate-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M10 19l-7-7m0 0l7-7m-7 7h18" />
              </svg>
              <span>Back</span>
            </button>
            <div className="flex items-center gap-2.5">
              <h2 id="modal-title" className="text-xl font-bold tracking-tight text-slate-900">
                {collection.folderName}
              </h2>
              <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-bold capitalize border ${collection.status === "passed" ? "bg-emerald-100 text-emerald-800 border-emerald-200" : "bg-rose-100 text-rose-800 border-rose-200"
                }`}>
                <span className={`h-1.5 w-1.5 rounded-full ${collection.status === "passed" ? "bg-emerald-600" : "bg-rose-600"}`} />
                {collection.status}
              </span>
            </div>
          </div>

          <div className="flex items-center gap-2.5">
            <button
              type="button"
              onClick={openHtmlReport}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3.5 py-2 text-xs font-semibold text-slate-700 shadow-2xs hover:bg-slate-50 active:bg-slate-100 transition"
              title="Open full Newman HTML report"
            >
              <svg className="h-4 w-4 text-slate-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
              </svg>
              View Newman Report
            </button>
          </div>
        </div>

        {/* Compact KPI Statistics Ribbon */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5 border-b border-slate-200 bg-slate-50/70 px-6 py-2.5 text-xs shrink-0">
          <div className="flex items-center justify-between rounded-lg bg-white px-3 py-2 border border-slate-200 shadow-2xs">
            <span className="font-medium text-slate-500">Total Endpoints:</span>
            <span className="text-sm font-bold text-slate-900">{totalReqs}</span>
          </div>
          <div className="flex items-center justify-between rounded-lg bg-white px-3 py-2 border border-slate-200 shadow-2xs">
            <span className="font-medium text-slate-500">Passed Requests:</span>
            <span className="text-sm font-bold text-emerald-800">{totalReqs > 0 ? `${passedReqs} / ${totalReqs}` : "0"}</span>
          </div>
          <div className="flex items-center justify-between rounded-lg bg-white px-3 py-2 border border-slate-200 shadow-2xs">
            <span className="font-medium text-slate-500">Failed Requests:</span>
            <span className={`text-sm font-bold ${failedReqs > 0 ? "text-rose-700" : "text-slate-900"}`}>{failedReqs}</span>
          </div>
          <div className="flex items-center justify-between rounded-lg bg-white px-3 py-2 border border-slate-200 shadow-2xs">
            <span className="font-medium text-slate-500">Avg Response Time:</span>
            <span className="text-sm font-bold text-slate-900">{totalReqs > 0 ? `${avgLatency} ms` : "—"}</span>
          </div>
        </div>

        {/* Search, Filter & View Controls */}
        <div className="border-b border-slate-200 bg-white px-6 py-2.5 space-y-2 shrink-0">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <div className="relative flex-1">
              <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3">
                <svg className="h-4 w-4 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
              </div>
              <input
                type="search"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search"
                className="w-full rounded-lg border border-slate-300 bg-white pl-9 pr-3.5 py-1.5 text-xs text-slate-900 placeholder-slate-400 focus:border-slate-500 focus:outline-none"
              />
            </div>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as typeof statusFilter)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 focus:border-slate-500 focus:outline-none"
            >
              <option value="all">All Results ({result?.executions.length ?? 0})</option>
              <option value="passed">Passed Only</option>
              <option value="failed">Failed Only</option>
            </select>
            <select
              value={selectedMethod}
              onChange={(e) => setSelectedMethod(e.target.value)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 focus:border-slate-500 focus:outline-none"
            >
              <option value="all">All HTTP Methods</option>
              <option value="GET">GET</option>
              <option value="POST">POST</option>
              <option value="PUT">PUT</option>
              <option value="PATCH">PATCH</option>
              <option value="DELETE">DELETE</option>
            </select>
            <div className="flex rounded-lg border border-slate-200 bg-slate-100 p-0.5 text-xs">
              <button
                type="button"
                onClick={() => setViewMode("cards")}
                className={`rounded-md px-2.5 py-1 font-bold transition ${viewMode === "cards" ? "bg-white text-slate-900 shadow-xs" : "text-slate-600 hover:text-slate-900"
                  }`}
                title="Detailed Cards View"
              >
                Cards
              </button>
              <button
                type="button"
                onClick={() => setViewMode("compact")}
                className={`rounded-md px-2.5 py-1 font-bold transition ${viewMode === "compact" ? "bg-white text-slate-900 shadow-xs" : "text-slate-600 hover:text-slate-900"
                  }`}
                title="Dense Table View"
              >
                Table
              </button>
            </div>
          </div>

          {/* Horizontally Scrollable Folder Tabs */}
          {folders.length > 0 && (
            <div className="flex items-center gap-1.5 overflow-x-auto pb-1 pt-0.5 no-scrollbar">
              <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400 shrink-0 mr-1">
                Folder:
              </span>
              <button
                type="button"
                onClick={() => setSelectedFolder("all")}
                className={`rounded-lg px-3 py-1 text-xs font-bold shrink-0 transition ${selectedFolder === "all"
                    ? "bg-slate-900 text-white shadow-xs"
                    : "bg-slate-100 border border-slate-200 text-slate-700 hover:bg-slate-200"
                  }`}
              >
                All ({result?.executions.length ?? 0})
              </button>
              {folders.map((folder) => {
                const count = result?.executions.filter((e) => e.folder === folder).length ?? 0;
                return (
                  <button
                    key={folder}
                    type="button"
                    onClick={() => setSelectedFolder(folder)}
                    className={`rounded-lg px-3 py-1 text-xs font-medium shrink-0 transition ${selectedFolder === folder
                        ? "bg-slate-900 text-white font-bold shadow-xs"
                        : "bg-slate-100 border border-slate-200 text-slate-700 hover:bg-slate-200"
                      }`}
                  >
                    {folder} <span className="opacity-75 font-mono">({count})</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Endpoint List Body */}
        <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6 space-y-3 bg-slate-100/70">
          {loading && (
            <div className="flex flex-col items-center justify-center py-20 text-slate-500">
              <svg className="h-10 w-10 animate-spin text-cyan-600" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
              </svg>
              <p className="mt-3 text-sm font-semibold text-slate-700">Loading collection endpoints…</p>
            </div>
          )}

          {!loading && !result && (
            <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-12 text-center text-slate-500">
              <p className="font-bold text-slate-800 text-base">No execution data available yet</p>
              <p className="mt-1 text-xs">Run this collection to view endpoint metrics and live assertions.</p>
            </div>
          )}

          {!loading && result && filteredExecutions.length === 0 && (
            <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-12 text-center text-slate-500">
              <p className="font-bold text-slate-800 text-base">
                {result.executions.length === 0 ? "No endpoints configured" : "No matching endpoints found"}
              </p>
              <p className="mt-1 text-xs">
                {result.executions.length === 0 ? "This collection currently has no API endpoints." : "Try clearing or adjusting your search filters above."}
              </p>
            </div>
          )}

          {/* Detailed Cards View (3 Cards per Row) */}
          {!loading && result && filteredExecutions.length > 0 && viewMode === "cards" && (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3.5 items-stretch">
              {filteredExecutions.map((execution, index) => {
                const methodClass = methodBadgeStyles[execution.method.toUpperCase()] || "bg-slate-800 text-white";
                const isSuccessCode = execution.statusCode && execution.statusCode >= 200 && execution.statusCode < 300;

                return (
                  <div
                    key={`${execution.name}-${index}`}
                    className={`flex flex-col justify-between rounded-xl border bg-white p-3.5 shadow-xs transition hover:shadow-md ${execution.failed ? "border-rose-300 bg-rose-50/30" : "border-slate-200 hover:border-slate-300"
                      }`}
                  >
                    <div>
                      {/* Top Row: Method Badge, Status Code & Status Tag */}
                      <div className="flex items-center justify-between gap-1.5">
                        <div className="flex items-center gap-1.5 min-w-0">
                          <span className={`rounded-md px-2 py-0.5 font-mono text-[11px] font-bold shrink-0 ${methodClass}`}>
                            {execution.method}
                          </span>
                          <span
                            className={`rounded-md px-2 py-0.5 font-mono text-[11px] font-bold border shrink-0 ${isSuccessCode
                                ? "bg-slate-50 text-slate-800 border-slate-200"
                                : "bg-rose-50 text-rose-800 border-rose-300"
                              }`}
                          >
                            {execution.statusCode !== null ? `HTTP ${execution.statusCode}` : "No response"}
                          </span>
                        </div>
                        <span
                          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold capitalize border shrink-0 ${execution.failed
                              ? "bg-rose-100 text-rose-800 border-rose-200"
                              : "bg-emerald-100 text-emerald-800 border-emerald-200"
                            }`}
                        >
                          <span className={`h-1.5 w-1.5 rounded-full ${execution.failed ? "bg-rose-600" : "bg-emerald-600"}`} />
                          {execution.failed ? "failed" : "passed"}
                        </span>
                      </div>

                      {/* Endpoint Title */}
                      <h3 className="mt-2 font-bold text-slate-900 text-sm leading-snug" title={execution.name}>
                        {execution.name}
                      </h3>

                      {/* Folder Path & URL */}
                      <div className="mt-2 flex flex-col gap-1 text-xs">
                        {execution.folder && (
                          <div className="flex items-center gap-1.5 text-slate-600 min-w-0">
                            <svg className="h-3.5 w-3.5 text-slate-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                            </svg>
                            <span className="truncate font-medium text-[11px] text-slate-600 bg-slate-100 px-2 py-0.5 rounded-md border border-slate-200">
                              {execution.folder}
                            </span>
                          </div>
                        )}
                        {execution.url && (
                          <div className="truncate font-mono text-slate-400 text-[10px]" title={execution.url}>
                            {execution.url}
                          </div>
                        )}
                      </div>

                      {/* Metrics Row: Latency, Payload, Assertions */}
                      <div className="mt-2.5 grid grid-cols-3 gap-1 rounded-lg bg-slate-50 p-2 text-center text-xs border border-slate-100 text-slate-700">
                        <div className="flex flex-col">
                          <span className="text-slate-400 text-[10px] font-medium">Latency</span>
                          <span className="font-bold text-slate-900 text-[11px]">{formatDuration(execution.responseTimeMs)}</span>
                        </div>
                        <div className="flex flex-col border-x border-slate-200/80">
                          <span className="text-slate-400 text-[10px] font-medium">Payload</span>
                          <span className="font-bold text-slate-900 text-[11px]">{formatSize(execution.responseSize)}</span>
                        </div>
                        <div className="flex flex-col">
                          <span className="text-slate-400 text-[10px] font-medium">Assertions</span>
                          <span className={`font-bold text-[11px] ${execution.failed ? "text-rose-700" : "text-emerald-800"}`}>
                            {execution.assertions.filter((a) => a.passed).length}/{execution.assertions.length} Passed
                          </span>
                        </div>
                      </div>
                    </div>

                    {/* Assertion Checklist */}
                    {execution.assertions.length > 0 && (
                      <div className="mt-2.5 flex-1 rounded-lg bg-slate-50/80 border border-slate-200 p-2.5">
                        <p className="text-[10px] font-bold uppercase tracking-wider text-slate-500 mb-1.5">
                          Verified Test Assertions
                        </p>
                        <ul className="space-y-1 text-xs">
                          {execution.assertions.map((assertion, assertionIndex) => (
                            <li
                              key={`${assertion.name}-${assertionIndex}`}
                              className={`flex flex-col gap-0.5 rounded-md p-1.5 text-[11px] ${assertion.passed
                                  ? "text-slate-800"
                                  : "bg-rose-50 border border-rose-200 text-rose-950 font-medium"
                                }`}
                            >
                              <div className="flex items-start gap-1.5">
                                <span className={`flex h-3.5 w-3.5 items-center justify-center rounded-full text-[9px] font-bold text-white shrink-0 mt-0.5 ${assertion.passed ? "bg-emerald-600" : "bg-rose-600"
                                  }`}>
                                  {assertion.passed ? "✓" : "✕"}
                                </span>
                                <span className="leading-tight">{assertion.name}</span>
                              </div>
                              {assertion.errorMessage && (
                                <p className="pl-5 font-mono text-[10px] text-rose-700 leading-tight break-all">
                                  Assertion Error: {assertion.errorMessage}
                                </p>
                              )}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {/* Dense Table View */}
          {!loading && result && filteredExecutions.length > 0 && viewMode === "compact" && (
            <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-xs">
              <table className="min-w-full text-left text-xs">
                <thead className="border-b border-slate-200 bg-slate-50 text-[11px] font-bold uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="w-14 px-4 py-3 text-center">S.No</th>
                    <th className="px-4 py-3">Method</th>
                    <th className="px-4 py-3">Endpoint Name & Folder</th>
                    <th className="px-4 py-3">Status</th>
                    <th className="px-4 py-3">Latency</th>
                    <th className="px-4 py-3">Size</th>
                    <th className="px-4 py-3">Assertions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {filteredExecutions.map((execution, index) => {
                    const methodClass = methodBadgeStyles[execution.method.toUpperCase()] || "bg-slate-800 text-white";
                    return (
                      <tr key={`${execution.name}-${index}`} className={`hover:bg-slate-50 ${execution.failed ? "bg-rose-50/30" : ""}`}>
                        <td className="w-14 px-4 py-2.5 text-center font-medium text-slate-500">{index + 1}</td>
                        <td className="px-4 py-2.5">
                          <span className={`rounded px-2 py-0.5 font-mono text-[11px] ${methodClass}`}>
                            {execution.method}
                          </span>
                        </td>
                        <td className="px-4 py-2.5">
                          <p className="font-bold text-slate-900">{execution.name}</p>
                          <p className="text-[11px] text-slate-500 font-mono">{execution.folder ?? "Root"}</p>
                        </td>
                        <td className="px-4 py-2.5">
                          <span className={`inline-flex rounded px-2 py-0.5 font-mono text-[11px] font-bold ${execution.failed
                              ? "bg-rose-100 text-rose-800 border border-rose-200"
                              : "bg-emerald-100 text-emerald-800"
                            }`}>
                            {execution.statusCode !== null ? `HTTP ${execution.statusCode}` : "No response"}
                          </span>
                        </td>
                        <td className="px-4 py-2.5 font-bold text-slate-800">
                          {formatDuration(execution.responseTimeMs)}
                        </td>
                        <td className="px-4 py-2.5 font-mono text-slate-600">
                          {formatSize(execution.responseSize)}
                        </td>
                        <td className="px-4 py-2.5">
                          <span className={`font-bold ${execution.failed ? "text-rose-700" : "text-emerald-800"}`}>
                            {execution.assertions.filter((a) => a.passed).length}/{execution.assertions.length} Passed
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Modal Footer */}
        <div className="flex items-center justify-between border-t border-slate-200 bg-white px-6 py-3 text-xs text-slate-600 shrink-0">
          <span className="font-medium">
            Showing <strong className="text-slate-900">{filteredExecutions.length}</strong> of <strong className="text-slate-900">{result?.executions.length ?? 0}</strong> endpoints
          </span>
        </div>
      </div>
    </div>
  );
}
