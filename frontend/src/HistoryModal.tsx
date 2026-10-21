import { useEffect, useRef } from "react";
import { openAuthenticatedReport } from "./api";
import { formatDate, formatDuration } from "./format";
import type { Collection, HistoryRunSummary } from "./types";

interface HistoryModalProps {
  collection: Collection;
  history: HistoryRunSummary[];
  loading: boolean;
  onClose: () => void;
}

function openRunReport(reportUrl: string): void {
  const reportTab = window.open("about:blank", "_blank");
  if (reportTab) reportTab.opener = null;
  void openAuthenticatedReport(reportUrl, reportTab).catch((error: unknown) => {
    reportTab?.close();
    window.alert(error instanceof Error ? error.message : "Could not open this report.");
  });
}

export function HistoryModal({ collection, history, loading, onClose }: HistoryModalProps) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeButtonRef.current?.focus();
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 p-3 backdrop-blur-xs sm:p-5 md:p-6"
      role="dialog"
      aria-modal="true"
      aria-labelledby="history-modal-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="relative flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
        {/* Modal Top Header */}
        <div className="flex flex-col gap-3 border-b border-slate-200 bg-white px-6 py-4 text-slate-900 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-slate-100 text-slate-700 border border-slate-200 font-bold">
              <svg className="h-5 w-5 text-slate-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </div>
            <div className="flex items-center gap-2.5">
              <h2 id="history-modal-title" className="text-xl font-bold tracking-tight text-slate-900">
                {collection.folderName}
              </h2>
              <span className="inline-flex rounded-full bg-slate-100 px-2.5 py-0.5 text-xs font-semibold text-slate-700 border border-slate-200">
                Last 5 Test Runs
              </span>
            </div>
          </div>

          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            className="inline-flex items-center gap-1 rounded-lg bg-slate-900 px-3.5 py-2 text-xs font-semibold text-white shadow-2xs hover:bg-slate-800 active:bg-slate-700 transition"
          >
            <svg className="h-4 w-4 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 18L18 6M6 6l12 12" />
            </svg>
            Close
          </button>
        </div>

        {/* Info Banner */}
        <div className="bg-slate-50 border-b border-slate-200 px-6 py-2.5 text-xs text-slate-600 flex items-center justify-between">
          <div className="flex items-center gap-2 font-medium">
            <svg className="h-4 w-4 text-slate-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <span>Archiving the <strong>last 5 test runs</strong>. Running a new test appends the latest result and removes the 5th oldest run.</span>
          </div>
          <span className="font-mono text-[11px] text-slate-500 shrink-0 ml-2">FIFO History Queue</span>
        </div>

        {/* History Runs List Body */}
        <div className="min-h-0 flex-1 overflow-y-auto p-6 space-y-4 bg-slate-50/50">
          {loading && (
            <div className="flex flex-col items-center justify-center py-20 text-slate-500">
              <svg className="h-10 w-10 animate-spin text-slate-600" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
              </svg>
              <p className="mt-3 text-sm font-semibold text-slate-700">Loading test execution history…</p>
            </div>
          )}

          {!loading && history.length === 0 && (
            <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-12 text-center text-slate-500">
              <p className="font-bold text-slate-800 text-base">No history records found yet</p>
              <p className="mt-1 text-xs">Run this collection to record your first execution in history.</p>
            </div>
          )}

          {!loading && history.map((run, index) => {
            const isLatest = index === 0;
            const passed = run.status === "passed";

            return (
              <div
                key={run.runId}
                className={`rounded-xl border bg-white p-5 shadow-xs transition hover:shadow-md ${
                  isLatest
                    ? "border-slate-300 ring-1 ring-slate-300"
                    : "border-slate-200 hover:border-slate-300"
                }`}
              >
                {/* Header of Run Card */}
                <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-100 pb-3">
                  <div className="flex flex-wrap items-center gap-2.5">
                    <span
                      className={`inline-flex items-center rounded-lg px-2.5 py-1 text-xs font-bold ${
                        isLatest
                          ? "bg-slate-900 text-white shadow-xs"
                          : "bg-slate-100 text-slate-700 border border-slate-200"
                      }`}
                    >
                      Run #{run.runNumber} {isLatest && "(Latest)"}
                    </span>
                    <span
                      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-bold capitalize border ${
                        passed
                          ? "bg-emerald-100 text-emerald-800 border-emerald-200"
                          : "bg-rose-100 text-rose-800 border-rose-200"
                      }`}
                    >
                      <span className={`h-1.5 w-1.5 rounded-full ${passed ? "bg-emerald-600" : "bg-rose-600"}`} />
                      {run.status}
                    </span>
                  </div>

                  <div className="flex items-center gap-3 text-xs text-slate-500">
                    <span className="flex items-center gap-1">
                      <svg className="h-3.5 w-3.5 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
                      </svg>
                      <strong>{formatDate(run.completedAt)}</strong>
                    </span>
                    <span className="text-slate-300">•</span>
                    <span className="font-mono text-slate-600">
                      Duration: <strong>{formatDuration(run.durationMs)}</strong>
                    </span>
                  </div>
                </div>

                {/* Metrics & Actions Row */}
                <div className="mt-3.5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  {/* Metric Chips */}
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="rounded-lg bg-slate-50 px-3 py-1.5 font-medium text-slate-700 border border-slate-200">
                      Total APIs: <strong className="text-slate-900">{run.totalApis}</strong>
                    </span>
                    <span className="rounded-lg bg-slate-50 px-3 py-1.5 font-medium text-slate-700 border border-slate-200">
                      Assertions: <strong className="text-emerald-800">{run.assertions.passed} / {run.assertions.total} Passed</strong>
                    </span>
                    <span className="rounded-lg bg-slate-50 px-3 py-1.5 font-medium text-slate-700 border border-slate-200">
                      Avg Latency: <strong className="text-slate-900">{run.avgLatencyMs} ms</strong>
                    </span>
                  </div>

                  {/* Actions for this specific run */}
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => openRunReport(run.reportUrl)}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3.5 py-1.5 text-xs font-semibold text-slate-700 shadow-2xs hover:bg-slate-50 active:bg-slate-100 transition"
                      title="Open full Newman HTML execution report for this historical test run"
                    >
                      <svg className="h-3.5 w-3.5 text-slate-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                      </svg>
                      Newman Report
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Modal Footer */}
        <div className="flex items-center justify-between border-t border-slate-200 bg-white px-6 py-3 text-xs text-slate-600">
          <span className="font-medium">
            Stored <strong className="text-slate-900">{history.length}</strong> of maximum <strong>5</strong> test runs in history
          </span>
        </div>
      </div>
    </div>
  );
}
