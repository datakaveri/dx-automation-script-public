import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { Drawer } from "./Drawer";
import { HistoryModal } from "./HistoryModal";
import { AutoRerunModal } from "./AutoRerunModal";
import { ToastContainer } from "./Toast";
import { LoginPage } from "./LoginPage";
import { keycloak, keycloakConfigured } from "./keycloak";
import { formatDate, formatDuration } from "./format";
import type { Collection, CollectionStatus, DashboardKpis, HistoryRunSummary, RunResult, SkippedFile } from "./types";

const statusClasses: Record<CollectionStatus, string> = {
  idle: "bg-slate-100 text-slate-700 border-slate-200",
  running: "bg-amber-100 text-amber-800 border-amber-200",
  passed: "bg-emerald-100 text-emerald-800 border-emerald-200",
  failed: "bg-rose-100 text-rose-800 border-rose-200",
  error: "bg-orange-100 text-orange-800 border-orange-200"
};

function StatusBadge({ status }: { status: CollectionStatus }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-bold capitalize border ${statusClasses[status]}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${status === "passed" ? "bg-emerald-600" : status === "running" ? "bg-amber-600 animate-pulse" : "bg-rose-600"}`} />
      {status}
    </span>
  );
}

export default function App() {
  const [currentUser, setCurrentUser] = useState<string | null>(() => keycloak?.authenticated ? (keycloak.tokenParsed?.email ?? keycloak.tokenParsed?.preferred_username ?? "Keycloak user") : null);
  const [currentUserName, setCurrentUserName] = useState<string | null>(() => keycloak?.authenticated ? (keycloak.tokenParsed?.name ?? null) : null);
  const [loginError, setLoginError] = useState<string | null>(() => keycloakConfigured ? null : "Keycloak is not configured. Set VITE_KEYCLOAK_URL, VITE_KEYCLOAK_REALM, and VITE_KEYCLOAK_CLIENT_ID.");
  const [isQaAdmin, setIsQaAdmin] = useState(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [collections, setCollections] = useState<Collection[] | null>(null);
  const [kpis, setKpis] = useState<DashboardKpis | null>(null);
  const [skipped, setSkipped] = useState<SkippedFile[]>([]);
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<"all" | CollectionStatus>("all");
  const [selected, setSelected] = useState<Collection | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);
  const [resultLoading, setResultLoading] = useState(false);
  const [historyTarget, setHistoryTarget] = useState<Collection | null>(null);
  const [historyList, setHistoryList] = useState<HistoryRunSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [activeRuns, setActiveRuns] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const [openActionMenuId, setOpenActionMenuId] = useState<string | null>(null);
  const [isProfileMenuOpen, setIsProfileMenuOpen] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [isBulkRunning, setIsBulkRunning] = useState(false);
  const [bulkRunTrackedIds, setBulkRunTrackedIds] = useState<Set<string>>(new Set());
  const headerCheckboxRef = useRef<HTMLInputElement>(null);
  const profileMenuRef = useRef<HTMLDivElement>(null);
  const handleLogin = () => {
    if (!keycloak) return;
    setLoginError(null);
    void keycloak.login().catch(() => setLoginError("Could not start Keycloak sign-in. Check the client and redirect URI configuration."));
  };

  const handleLogout = () => {
    setIsProfileMenuOpen(false);
    if (keycloak) void keycloak.logout({ redirectUri: globalThis.location.origin });
  };

  useEffect(() => {
    if (!isProfileMenuOpen) return;

    const closeOnOutsideClick = (event: MouseEvent) => {
      if (event.target instanceof Node && !profileMenuRef.current?.contains(event.target)) {
        setIsProfileMenuOpen(false);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setIsProfileMenuOpen(false);
    };

    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [isProfileMenuOpen]);

  useEffect(() => {
    if (!keycloak?.authenticated) {
      setCurrentUser(null);
      setCurrentUserName(null);
      setIsQaAdmin(false);
      return;
    }
    const claims = keycloak.tokenParsed;
    setCurrentUser(claims?.email ?? claims?.preferred_username ?? "Keycloak user");
    setCurrentUserName(claims?.name ?? null);
    void api.me().then(({ user }) => {
      setCurrentUser(user.email ?? user.username ?? user.name ?? "Keycloak user");
      setCurrentUserName(user.name ?? null);
      setIsQaAdmin(user.roles.realm.includes("qa_admin"));
    }).catch((requestError: unknown) => {
      setError(requestError instanceof Error ? requestError.message : "Could not load your Keycloak roles.");
    });
  }, []);

  const refresh = useCallback(async (includeSkipped = false) => {
    try {
      const [dashboard, nextSkipped] = await Promise.all([
        api.dashboard(),
        includeSkipped ? api.skipped() : Promise.resolve(undefined)
      ]);
      setCollections(dashboard.data.collections);
      setKpis(dashboard.data.kpis);
      setActiveRuns((current) => {
        const next = { ...current };
        for (const collection of dashboard.data.collections) {
          if (collection.status === "running" && collection.runId) next[collection.id] = collection.runId;
        }
        return next;
      });
      if (nextSkipped) setSkipped(nextSkipped);
      setLastRefreshed(new Date());
      setError(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not refresh dashboard data.");
    }
  }, []);

  useEffect(() => {
    if (currentUser) {
      void refresh(true);
    }
  }, [currentUser, refresh]);

  useEffect(() => {
    const entries = Object.entries(activeRuns);
    if (entries.length === 0) return;

    let polling = false;
    let cancelled = false;

    const pollStatuses = async () => {
      if (polling || cancelled) return;
      polling = true;
      try {
        const statuses = await Promise.all(entries.map(async ([collectionId, runId]) => {
          try {
            const status = await api.runStatus(runId);
            return { collectionId, status: status.data.status, failed: false };
          } catch (requestError) {
            setError(requestError instanceof Error ? requestError.message : "Could not read collection run status.");
            return { collectionId, status: "error" as const, failed: true };
          }
        }));

        if (cancelled) return;

        const completedCollectionIds = statuses
          .filter(({ status, failed }) => failed || ["passed", "failed", "error"].includes(status))
          .map(({ collectionId }) => collectionId);

        setCollections((current) => current
          ? current.map((collection) => {
            const nextStatus = statuses.find((item) => item.collectionId === collection.id)?.status;
            return nextStatus ? { ...collection, status: nextStatus } : collection;
          })
          : current);

        if (completedCollectionIds.length > 0) {
          setActiveRuns((current) => {
            const next = { ...current };
            for (const collectionId of completedCollectionIds) delete next[collectionId];
            return next;
          });

          setBulkRunTrackedIds((prevTracked) => {
            if (prevTracked.size === 0) {
              void refresh();
              return prevTracked;
            }
            const nextTracked = new Set(prevTracked);
            for (const id of completedCollectionIds) {
              nextTracked.delete(id);
            }
            if (nextTracked.size === 0) {
              setIsBulkRunning(false);
              setSelectedIds(new Set());
              void refresh();
            }
            return nextTracked;
          });
        }
      } finally {
        polling = false;
      }
    };

    void pollStatuses();
    const timer = window.setInterval(() => void pollStatuses(), 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [activeRuns, refresh]);

  useEffect(() => {
    const handleOutsideClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && !target.closest(".action-menu-dropdown-container")) {
        setOpenActionMenuId(null);
      }
    };
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpenActionMenuId(null);
      }
    };
    document.addEventListener("mousedown", handleOutsideClick);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleOutsideClick);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, []);

  const filteredCollections = useMemo(() => {
    const query = search.trim().toLowerCase();
    return (collections ?? []).filter((collection) => {
      const matchesSearch = !query || collection.folderName.toLowerCase().includes(query);
      return matchesSearch && (status === "all" || collection.status === status);
    });
  }, [collections, search, status]);

  const displayedCollectionIds = useMemo(() => {
    return filteredCollections.map((c) => c.id);
  }, [filteredCollections]);

  const selectedDisplayedCount = useMemo(() => {
    return displayedCollectionIds.filter((id) => selectedIds.has(id)).length;
  }, [displayedCollectionIds, selectedIds]);

  const isAllSelected = displayedCollectionIds.length > 0 && selectedDisplayedCount === displayedCollectionIds.length;
  const isIndeterminate = selectedDisplayedCount > 0 && selectedDisplayedCount < displayedCollectionIds.length;

  useEffect(() => {
    if (headerCheckboxRef.current) {
      headerCheckboxRef.current.indeterminate = isIndeterminate;
    }
  }, [isIndeterminate]);

  const handleToggleAll = () => {
    setOpenActionMenuId(null);
    if (isAllSelected) {
      setSelectedIds((prev) => {
        const next = new Set(prev);
        for (const id of displayedCollectionIds) {
          next.delete(id);
        }
        return next;
      });
    } else {
      setSelectedIds((prev) => {
        const next = new Set(prev);
        for (const id of displayedCollectionIds) {
          next.add(id);
        }
        return next;
      });
    }
  };

  const handleToggleRow = (id: string, event: React.ChangeEvent<HTMLInputElement>) => {
    event.stopPropagation();
    setOpenActionMenuId(null);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const handleRunSelected = async () => {
    if (selectedIds.size === 0 || isBulkRunning) return;
    const selectedCollectionIds = Array.from(selectedIds);
    setSelectedIds(new Set());
    setIsBulkRunning(true);
    setError(null);
    try {
      const response = await api.runSelected(selectedCollectionIds);
      const queued = response.data?.queued ?? [];
      const skipped = response.data?.skipped ?? [];

      if (skipped.length > 0) {
        const skippedDetails = skipped.map(
          (item) => `Collection "${item.collectionId}" skipped: ${item.reason.replace(/_/g, " ")}.`
        ).join(" ");
        setError(skippedDetails);

        setCollections((current) => current ? current.map((c) => {
          const skippedItem = skipped.find((s) => s.collectionId === c.id);
          if (skippedItem?.reason === "invalid_collection") {
            return { ...c, status: "error" };
          }
          return c;
        }) : current);
      }

      if (queued.length > 0) {
        setCollections((current) => current ? current.map((c) => {
          const queuedItem = queued.find((q) => q.collectionId === c.id);
          return queuedItem ? { ...c, status: "running", runId: queuedItem.runId } : c;
        }) : current);

        setActiveRuns((current) => {
          const next = { ...current };
          for (const item of queued) {
            next[item.collectionId] = item.runId;
          }
          return next;
        });

        setBulkRunTrackedIds(new Set(queued.map((q) => q.collectionId)));
      } else {
        setIsBulkRunning(false);
        setSelectedIds(new Set());
        await refresh();
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Failed to run selected collections.");
      setIsBulkRunning(false);
      setBulkRunTrackedIds(new Set());
      await refresh();
    }
  };

  async function showResult(collection: Collection) {
    setSelected(collection);
    setResult(null);
    setResultLoading(true);
    try {
      setResult((await api.result(collection.id)).result);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not load run details.");
    } finally {
      setResultLoading(false);
    }
  }

  async function openHistory(collection: Collection) {
    setHistoryTarget(collection);
    setHistoryList([]);
    setHistoryLoading(true);
    try {
      const historyData = await api.history(collection.id);
      setHistoryList(historyData.data.runs);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not load test execution history.");
    } finally {
      setHistoryLoading(false);
    }
  }

  async function run(id: string) {
    setSelectedIds(new Set());
    try {
      setCollections((prev) => prev ? prev.map((c) => c.id === id ? { ...c, status: "running" } : c) : prev);
      const runResponse = await api.run(id);
      setActiveRuns((current) => ({ ...current, [id]: runResponse.data.runId }));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not start collection.");
      await refresh();
    }
  }

  const cards = kpis
    ? [
      { label: "Total Collections", value: kpis.totalCollections, highlight: "text-slate-900" },
      { label: "Passed Collections", value: kpis.passedCollections, highlight: "text-emerald-800" },
      { label: "Failed Collections", value: kpis.failedCollections, highlight: kpis.failedCollections > 0 ? "text-rose-700" : "text-slate-900" },
      { label: "Total APIs", value: `${kpis.totalApis} APIs`, highlight: "text-sky-700" },
      { label: "Failed Assertions", value: kpis.failedAssertions, highlight: kpis.failedAssertions > 0 ? "text-rose-700" : "text-emerald-800" }
    ]
    : [];

  if (!currentUser) {
    return <LoginPage onLogin={handleLogin} error={loginError} />;
  }

  return (
    <main className="min-h-screen bg-white text-slate-900">
      {/* Top Banner / Navbar */}
      <header className="border-b border-slate-200 bg-white shadow-xs">
        <div className="mx-auto max-w-7xl xl:max-w-[1380px] px-4 py-5 sm:px-6 lg:px-8">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <h1 className="text-2xl font-black tracking-tight text-slate-900 sm:text-3xl">
                API Test Execution Dashboard
              </h1>
            </div>
            <div className="flex items-center gap-2.5">

              {isQaAdmin && (
                <button
                  type="button"
                  id="auto-rerun-settings-btn"
                  onClick={() => setIsSettingsOpen(true)}
                  className="group inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3.5 py-2 text-xs font-bold text-slate-700 shadow-xs hover:border-sky-300 hover:bg-sky-50 hover:text-sky-700 active:bg-sky-100 transition cursor-pointer"
                  title="Configure scheduled runs and email alerts"
                >
                  <svg className="h-3.5 w-3.5 text-slate-500 group-hover:text-sky-600 transition-colors" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  Auto Re-run Settings
                </button>
              )}

              <button
                type="button"
                onClick={() => void refresh(true)}
                className="group inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3.5 py-2 text-xs font-bold text-slate-700 shadow-xs hover:border-sky-300 hover:bg-sky-50 hover:text-sky-700 active:bg-sky-100 transition"
              >
                <svg className="h-3.5 w-3.5 text-slate-500 group-hover:text-sky-600 transition-colors" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                </svg>
                Refresh
              </button>
              <div className="relative" ref={profileMenuRef}>
                <button
                  type="button"
                  id="user-profile-menu-button"
                  aria-label="Open user profile menu"
                  aria-haspopup="menu"
                  aria-expanded={isProfileMenuOpen}
                  aria-controls="user-profile-menu"
                  onClick={() => setIsProfileMenuOpen((open) => !open)}
                  className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-slate-300 bg-white text-slate-600 shadow-xs transition hover:border-sky-400 hover:bg-sky-50 hover:text-sky-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600"
                  title="User profile"
                >
                  <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.5 20.1a7.5 7.5 0 0115 0A17.9 17.9 0 0112 21.75c-2.7 0-5.25-.6-7.5-1.65z" />
                  </svg>
                </button>
                {isProfileMenuOpen && (
                  <div
                    id="user-profile-menu"
                    role="menu"
                    aria-labelledby="user-profile-menu-button"
                    className="absolute right-0 z-50 mt-2 w-80 max-w-[calc(100vw-2rem)] overflow-hidden rounded-xl border border-slate-200 bg-white shadow-xl ring-1 ring-slate-900/5"
                  >
                    <div className="flex items-center gap-3 bg-slate-50/80 px-4 py-3">
                      <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-sky-100 text-sm font-bold text-sky-800" aria-hidden="true">
                        {(currentUserName ?? currentUser ?? "U").trim().charAt(0).toUpperCase()}
                      </span>
                      <div className="min-w-0">
                        {currentUserName && <p className="truncate text-sm font-semibold text-slate-900">{currentUserName}</p>}
                        <p className="truncate text-xs text-slate-500" title={currentUser ?? undefined}>{currentUser}</p>
                      </div>
                    </div>
                    <div className="border-t border-slate-100 p-2">
                      <button
                        type="button"
                        role="menuitem"
                        onClick={handleLogout}
                        className="inline-flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-700 transition hover:bg-rose-50 hover:text-rose-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600"
                      >
                        <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                        </svg>
                        Log out
                      </button>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      </header>

      <div className="mx-auto max-w-7xl xl:max-w-[1380px] px-4 py-6 sm:px-6 lg:px-8 space-y-6">
        {error && (
          <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-xs font-medium text-rose-900">
            {error}
          </div>
        )}

        {/* Metric Cards Grid */}
        <section aria-label="Summary" className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
          {cards.map((card) => (
            <div key={card.label} className="rounded-xl border border-slate-200 bg-white p-4 shadow-xs transition-all duration-150 hover:border-sky-300 hover:bg-sky-50/20 hover:shadow-sm">
              <p className="text-xs font-semibold uppercase tracking-wider text-slate-500">{card.label}</p>
              <p className={`mt-2 text-2xl font-black ${card.highlight}`}>{card.value}</p>
            </div>
          ))}
        </section>

        {/* Main Collections Table Card */}
        <section className="rounded-xl border border-slate-200 bg-white shadow-xs overflow-hidden">
          <div className="flex flex-col gap-2 border-b border-slate-200 bg-white p-4 sm:flex-row sm:items-center sm:justify-end">
            <div className="flex flex-wrap items-center gap-2.5 w-full sm:w-auto">
              <input
                id="collection-search"
                type="search"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search"
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-800 placeholder-slate-400 focus:border-slate-500 focus:outline-none"
              />
              <select
                id="status-filter"
                value={status}
                onChange={(event) => setStatus(event.target.value as typeof status)}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 focus:border-slate-500 focus:outline-none"
              >
                <option value="all">All Statuses</option>
                <option value="passed">Passed</option>
                <option value="failed">Failed</option>
                <option value="running">Running</option>
              </select>

              {isQaAdmin && (selectedIds.size > 0 || isBulkRunning) && (
                <button
                  type="button"
                  id="run-selected-btn"
                  disabled={isBulkRunning}
                  onClick={() => void handleRunSelected()}
                  className="group inline-flex items-center gap-1.5 rounded-lg border border-sky-600 bg-sky-600 px-3.5 py-1.5 text-xs font-bold text-white shadow-xs hover:bg-sky-700 active:bg-sky-800 disabled:cursor-not-allowed disabled:opacity-60 transition cursor-pointer"
                  title="Run selected collections"
                >
                  {isBulkRunning ? (
                    <svg className="h-3.5 w-3.5 animate-spin text-white" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                    </svg>
                  ) : (
                    <svg className="h-3.5 w-3.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                      <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                  )}
                  <span>{isBulkRunning ? "Running Selected..." : `Run Selected ${selectedIds.size}`}</span>
                </button>
              )}
            </div>
          </div>

          {collections === null && (
            <div role="status" className="p-12 text-center text-xs text-slate-500">
              Loading collection inventory…
            </div>
          )}

          {collections !== null && filteredCollections.length === 0 && (
            <div className="p-12 text-center text-xs text-slate-500">
              No collections match the current filters.
            </div>
          )}

          {filteredCollections.length > 0 && (
            <div className="overflow-x-auto">
              <table className="min-w-full text-left text-xs bg-white">
                <thead className="border-b border-slate-200 bg-slate-50/80 text-[11px] font-bold uppercase tracking-wider text-slate-500">
                  <tr>
                    {isQaAdmin && (<th className="w-10 px-3 py-3.5 text-center">
                      <input
                        type="checkbox"
                        id="select-all-collections"
                        aria-label="Select all collections"
                        ref={headerCheckboxRef}
                        checked={isAllSelected}
                        onChange={handleToggleAll}
                        className="h-4 w-4 rounded border-slate-300 text-sky-600 focus:ring-sky-500 cursor-pointer accent-sky-600 align-middle"
                      />
                    </th>)}
                    <th className="w-12 px-4 py-3.5 text-center">S.No</th>
                    <th className="px-5 py-3.5">Collection & File</th>
                    <th className="px-3.5 py-3.5 whitespace-nowrap">Environment</th>
                    <th className="px-3.5 py-3.5 whitespace-nowrap">Status</th>
                    <th className="px-3.5 py-3.5 whitespace-nowrap">APIs / Endpoints</th>
                    <th className="px-3.5 py-3.5 whitespace-nowrap">Total Assertion (Passed/Total)</th>
                    <th className="px-3.5 py-3.5 whitespace-nowrap">Total Test (Passed/Total)</th>
                    <th className="px-3.5 py-3.5 whitespace-nowrap">Last Run</th>
                    <th className="px-3.5 py-3.5 whitespace-nowrap">Duration</th>
                    <th className="sticky right-0 z-20 bg-slate-50 px-4 py-3.5 text-right whitespace-nowrap shadow-[-8px_0_12px_-8px_rgba(15,23,42,0.25)]">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {filteredCollections.map((collection, index) => {
                    const isSelected = selectedIds.has(collection.id);
                    const isRowActionDisabled = isSelected;
                    const opensUpward = filteredCollections.length <= 2 || index >= filteredCollections.length - 2;
                    return (
                      <tr
                        key={collection.id}
                        role="button"
                        tabIndex={0}
                        onClick={() => void showResult(collection)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            void showResult(collection);
                          }
                        }}
                        className={`group cursor-pointer transition-all duration-150 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-700 ${isSelected ? "bg-sky-50/50 hover:bg-sky-100/70" : "bg-white hover:bg-sky-100/60"
                          }`}
                      >
                        {isQaAdmin && (<td
                          className="w-10 px-3 py-3.5 text-center"
                          onClick={(e) => e.stopPropagation()}
                          onKeyDown={(e) => e.stopPropagation()}
                        >
                          <input
                            type="checkbox"
                            id={`select-collection-${collection.id}`}
                            aria-label={`Select collection ${collection.folderName}`}
                            checked={isSelected}
                            onChange={(e) => handleToggleRow(collection.id, e)}
                            className="h-4 w-4 rounded border-slate-300 text-sky-600 focus:ring-sky-500 cursor-pointer accent-sky-600 align-middle"
                          />
                        </td>)}
                        <td className="w-12 px-4 py-3.5 text-center font-bold text-slate-400 group-hover:text-sky-800 text-xs border-l-4 border-transparent group-hover:border-sky-500 transition-all whitespace-nowrap">
                          {index + 1}
                        </td>
                        <td className="max-w-72 px-5 py-3.5">
                          <p className="font-bold text-slate-900 text-sm group-hover:text-sky-950 transition-colors">{collection.folderName}</p>
                          <p className="mt-0.5 truncate text-[11px] font-mono text-slate-500" title={collection.collectionFile}>{collection.collectionFile}</p>
                        </td>
                        <td className="px-3.5 py-3 whitespace-nowrap">
                          {collection.environmentFile ? (
                            <span
                              title={collection.environmentFile}
                              className="inline-block max-w-44 truncate rounded-md bg-slate-100 px-2 py-0.5 font-mono text-[11px] text-slate-700 border border-slate-200 group-hover:bg-white group-hover:border-sky-300 group-hover:text-slate-900 shadow-2xs transition-colors align-middle"
                            >
                              {collection.environmentFile}
                            </span>
                          ) : (
                            <span className="text-slate-400">Default Environment</span>
                          )}
                        </td>
                        <td className="px-3.5 py-3 whitespace-nowrap">
                          <StatusBadge status={collection.status} />
                        </td>
                        <td className="px-3.5 py-3 font-medium text-slate-700 whitespace-nowrap">
                          <span><strong className="font-bold text-slate-900">{collection.apiCount}</strong> APIs</span>
                        </td>
                        <td className="px-3.5 py-3 font-medium text-slate-700 whitespace-nowrap">
                          <span>
                            <span className="text-emerald-800 font-bold">{collection.assertions.passed}</span>
                            <span className="text-slate-400"> / </span>
                            <span className="text-slate-700">{collection.assertions.total}</span>
                          </span>
                        </td>
                        <td className="px-3.5 py-3 font-medium text-slate-700 whitespace-nowrap">
                          <span>
                            <span className="text-emerald-800 font-bold">{collection.tests?.passed ?? 0}</span>
                            <span className="text-slate-400"> / </span>
                            <span className="text-slate-700">{collection.tests?.total ?? 0}</span>
                          </span>
                        </td>
                        <td className="px-3.5 py-3 text-slate-600 whitespace-nowrap">
                          {formatDate(collection.lastRunAt)}
                        </td>
                        <td className="px-3.5 py-3 font-mono text-slate-700 whitespace-nowrap">
                          {formatDuration(collection.durationMs)}
                        </td>
                        <td className={`sticky right-0 ${openActionMenuId === collection.id ? "z-30" : "z-10"
                          } px-4 py-3 text-right whitespace-nowrap shadow-[-8px_0_12px_-8px_rgba(15,23,42,0.2)] ${isSelected
                            ? "bg-sky-50 group-hover:bg-sky-100"
                            : "bg-white group-hover:bg-sky-100"
                          }`}>
                          <div className="relative inline-block text-left action-menu-dropdown-container" onClick={(e) => e.stopPropagation()}>
                            <button
                              type="button"
                              disabled={isRowActionDisabled}
                              onClick={() => setOpenActionMenuId(openActionMenuId === collection.id ? null : collection.id)}
                              className={`inline-flex h-8 w-8 items-center justify-center rounded-lg border transition-all ${isRowActionDisabled
                                ? "border-slate-200 bg-slate-50 text-slate-300 cursor-not-allowed opacity-50"
                                : openActionMenuId === collection.id
                                  ? "border-slate-300 bg-slate-100 text-slate-900 shadow-2xs"
                                  : "border-slate-200 bg-white text-slate-600 hover:border-sky-300 hover:bg-sky-50 hover:text-sky-700 active:bg-sky-100"
                                }`}
                              title={isRowActionDisabled ? "Actions are disabled for selected collections" : "Actions menu"}
                              aria-expanded={openActionMenuId === collection.id}
                              aria-label="Actions menu"
                            >
                              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M12 5v.01M12 12v.01M12 19v.01" />
                              </svg>
                            </button>

                            {openActionMenuId === collection.id && !isRowActionDisabled && (
                              <div
                                className={`absolute right-0 z-30 w-44 rounded-xl border border-slate-200 bg-white p-1.5 shadow-xl ring-1 ring-slate-900/5 ${opensUpward
                                  ? "bottom-full mb-2 origin-bottom-right"
                                  : "top-full mt-2 origin-top-right"
                                  }`}
                              >
                                {isQaAdmin && (<button
                                  type="button"
                                  disabled={
                                    collection.status === "running" ||
                                    isRowActionDisabled ||
                                    collection.actions?.canRerun === false
                                  }
                                  onClick={() => {
                                    setOpenActionMenuId(null);
                                    void run(collection.id);
                                  }}
                                  className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-xs font-semibold text-slate-700 hover:bg-sky-50 hover:text-sky-700 disabled:cursor-not-allowed disabled:opacity-50 transition-colors text-left"
                                  title={
                                    collection.status === "running"
                                      ? "A run is currently in progress"
                                      : collection.actions?.canRerun === false
                                        ? "Rerun is not available"
                                        : isRowActionDisabled
                                          ? "Actions are disabled for selected collections"
                                          : undefined
                                  }
                                >
                                  <svg className="h-4 w-4 text-slate-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                                    <path strokeLinecap="round" strokeLinejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z" />
                                    <path strokeLinecap="round" strokeLinejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                                  </svg>
                                  <span>{collection.lastRunAt ? "Re-run Collection" : "Run Collection"}</span>
                                </button>)}

                                <button
                                  type="button"
                                  disabled={isRowActionDisabled}
                                  onClick={() => {
                                    setOpenActionMenuId(null);
                                    void openHistory(collection);
                                  }}
                                  className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-xs font-semibold text-slate-700 hover:bg-sky-50 hover:text-sky-700 disabled:cursor-not-allowed disabled:opacity-50 transition-colors text-left"
                                >
                                  <svg className="h-4 w-4 text-slate-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                                  </svg>
                                  <span>View History</span>
                                </button>
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {skipped.length > 0 && (
          <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-xs">
            <h2 className="text-sm font-bold text-slate-900">Skipped Files & Unmatched Collections</h2>
            <ul className="mt-3 divide-y divide-slate-100 text-xs">
              {skipped.map((file) => (
                <li key={file.fileName} className="flex flex-col gap-1 py-2.5 sm:flex-row sm:justify-between">
                  <span className="font-semibold text-slate-800">{file.fileName}</span>
                  <span className="text-slate-500">{file.reason}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>

      {historyTarget && (
        <HistoryModal
          collection={historyTarget}
          history={historyList}
          loading={historyLoading}
          onClose={() => setHistoryTarget(null)}
        />
      )}

      {selected && (
        <Drawer
          collection={selected}
          result={result}
          loading={resultLoading}
          onClose={() => setSelected(null)}
        />
      )}

      {isQaAdmin && isSettingsOpen && (
        <AutoRerunModal
          onClose={() => setIsSettingsOpen(false)}
        />
      )}

      <ToastContainer />
    </main>
  );
}
