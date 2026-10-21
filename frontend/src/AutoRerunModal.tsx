import { useEffect, useRef, useState } from "react";
import { api, extractFieldErrors } from "./api";
import { toast } from "./Toast";
import type { AutoRerunInterval } from "./types";

interface AutoRerunModalProps {
  onClose: () => void;
}

const INTERVAL_OPTIONS: { value: AutoRerunInterval; label: string }[] = [
  { value: "off", label: "Off" },
  { value: "15m", label: "Every 15 minutes" },
  { value: "1h", label: "Every hour" },
  { value: "24h", label: "Every 24 hours" },
];

const EMAIL_REGEX = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;

export function AutoRerunModal({ onClose }: AutoRerunModalProps) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const emailInputRef = useRef<HTMLInputElement>(null);

  const [interval, setIntervalState] = useState<AutoRerunInterval>("off");
  const [recipients, setRecipients] = useState<string[]>([]);
  const [emailInput, setEmailInput] = useState("");

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [isSaving, setIsSaving] = useState(false);
  const [generalError, setGeneralError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string[]>>({});
  const [emailInputError, setEmailInputError] = useState<string | null>(null);

  const loadSettings = async () => {
    setLoading(true);
    setLoadError(null);
    setGeneralError(null);
    try {
      const response = await api.getAutoRerunSettings();
      const data = response.data ?? response;
      if (data.interval && INTERVAL_OPTIONS.some((opt) => opt.value === data.interval)) {
        setIntervalState(data.interval);
      } else {
        setIntervalState("off");
      }

      if (Array.isArray(data.recipients)) {
        setRecipients(data.recipients);
      } else {
        setRecipients([]);
      }
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to load auto re-run settings.";
      setLoadError(message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadSettings();
  }, []);

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

  const handleAddRecipient = () => {
    setEmailInputError(null);
    const trimmed = emailInput.trim();

    if (!trimmed) {
      setEmailInputError("Please enter an email address.");
      emailInputRef.current?.focus();
      return;
    }

    if (!EMAIL_REGEX.test(trimmed)) {
      setEmailInputError("Please enter a valid email address (e.g. qa-team@example.com).");
      emailInputRef.current?.focus();
      return;
    }

    if (recipients.some((r) => r.toLowerCase() === trimmed.toLowerCase())) {
      setEmailInputError("This email address is already added.");
      emailInputRef.current?.focus();
      return;
    }

    setRecipients((prev) => [...prev, trimmed]);
    setEmailInput("");
    setFieldErrors((prev) => {
      const next = { ...prev };
      delete next.recipients;
      return next;
    });
  };

  const handleRemoveRecipient = (emailToRemove: string) => {
    setRecipients((prev) => prev.filter((email) => email !== emailToRemove));
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setGeneralError(null);

    let currentRecipients = [...recipients];
    // If user typed a valid email and forgot to hit 'Add', add it automatically
    const trimmedInput = emailInput.trim();
    if (trimmedInput && EMAIL_REGEX.test(trimmedInput)) {
      if (!currentRecipients.some((r) => r.toLowerCase() === trimmedInput.toLowerCase())) {
        currentRecipients = [...currentRecipients, trimmedInput];
        setRecipients(currentRecipients);
        setEmailInput("");
        setEmailInputError(null);
      }
    }

    // Client-side validation: at least one recipient
    if (currentRecipients.length === 0) {
      const errorMsg = "At least one valid recipient is required before saving.";
      setFieldErrors((prev) => ({
        ...prev,
        recipients: [errorMsg],
      }));
      toast.error(errorMsg);
      return;
    }

    setIsSaving(true);
    setFieldErrors({});

    try {
      const response = await api.saveAutoRerunSettings({
        interval,
        recipients: currentRecipients,
      });

      const savedData = response.data ?? response;
      if (savedData.interval) setIntervalState(savedData.interval);
      if (Array.isArray(savedData.recipients)) setRecipients(savedData.recipients);

      toast.success("Auto re-run settings saved successfully.");
    } catch (err: unknown) {
      const extracted = extractFieldErrors(err);
      if (Object.keys(extracted).length > 0) {
        setFieldErrors(extracted);
      }
      const message =
        err instanceof Error ? err.message : "Failed to save auto re-run settings.";
      setGeneralError(message);
      const fieldErrorList = Object.values(extracted).flat();
      const toastMessage =
        fieldErrorList.length > 0
          ? `${message}: ${fieldErrorList.join(", ")}`
          : message;
      toast.error(toastMessage);
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 p-3 backdrop-blur-xs sm:p-5 md:p-6"
      role="dialog"
      aria-modal="true"
      aria-labelledby="auto-rerun-modal-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="relative flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
        {/* Modal Top Header */}
        <div className="flex flex-col gap-3 border-b border-slate-200 bg-white px-6 py-4 text-slate-900 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-slate-100 text-slate-700 border border-slate-200 font-bold">
              <svg className="h-5 w-5 text-slate-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </div>
            <div>
              <div className="flex items-center gap-2.5">
                <h2 id="auto-rerun-modal-title" className="text-xl font-bold tracking-tight text-slate-900">
                  Auto Re-run Settings
                </h2>
                <span className="inline-flex rounded-full bg-sky-50 px-2.5 py-0.5 text-xs font-semibold text-sky-700 border border-sky-200">
                  Scheduled Runs
                </span>
              </div>
              <p className="mt-0.5 text-xs text-slate-500">
                Configure background execution intervals and notification email recipients.
              </p>
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

        {/* Modal Body */}
        <div className="min-h-0 flex-1 overflow-y-auto p-6 space-y-6 bg-slate-50/50">
          {loading && (
            <div className="flex flex-col items-center justify-center py-16 text-slate-500">
              <svg className="h-9 w-9 animate-spin text-slate-600" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
              </svg>
              <p className="mt-3 text-sm font-semibold text-slate-700">Loading auto re-run settings…</p>
            </div>
          )}

          {!loading && loadError && (
            <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-4">
              <div className="flex items-start gap-3">
                <svg className="h-5 w-5 text-rose-600 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <div className="flex-1">
                  <h3 className="text-xs font-bold text-rose-900">Failed to load settings</h3>
                  <p className="mt-1 text-xs text-rose-700">{loadError}</p>
                  <button
                    type="button"
                    onClick={() => void loadSettings()}
                    className="mt-3 inline-flex items-center gap-1.5 rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-xs font-bold text-rose-800 shadow-2xs hover:bg-rose-50 transition"
                  >
                    Retry
                  </button>
                </div>
              </div>
            </div>
          )}

          {!loading && !loadError && (
            <form id="auto-rerun-form" onSubmit={handleSave} className="space-y-6">

              {/* General Error Message Banner */}
              {generalError && (
                <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-xs font-medium text-rose-900 flex items-center gap-2">
                  <svg className="h-4 w-4 text-rose-600 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span>{generalError}</span>
                </div>
              )}

              {/* Section 1: Interval Selection */}
              <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-xs space-y-3">
                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-1">
                  <div>
                    <label htmlFor="auto-rerun-interval" className="block text-xs font-bold uppercase tracking-wider text-slate-800">
                      Re-run Interval
                    </label>
                    <p className="text-xs text-slate-500 mt-0.5">
                      Choose how frequently automatic test runs should be triggered.
                    </p>
                  </div>
                </div>

                <div className="max-w-xs">
                  <select
                    id="auto-rerun-interval"
                    value={interval}
                    onChange={(e) => {
                      setIntervalState(e.target.value as AutoRerunInterval);
                      setFieldErrors((prev) => {
                        const next = { ...prev };
                        delete next.interval;
                        return next;
                      });
                    }}
                    className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-semibold text-slate-800 shadow-2xs focus:border-slate-800 focus:outline-none focus:ring-1 focus:ring-slate-800 cursor-pointer"
                  >
                    {INTERVAL_OPTIONS.map((opt) => (
                      <option key={opt.value} value={opt.value}>
                        {opt.label}
                      </option>
                    ))}
                  </select>
                </div>

                {fieldErrors.interval && (
                  <div className="space-y-0.5">
                    {fieldErrors.interval.map((err, i) => (
                      <p key={i} className="text-xs font-semibold text-rose-600">
                        {err}
                      </p>
                    ))}
                  </div>
                )}
              </div>

              {/* Section 2: QA Email Recipients */}
              <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-xs space-y-4">
                <div>
                  <div className="flex items-center justify-between">
                    <label htmlFor="recipient-email-input" className="block text-xs font-bold uppercase tracking-wider text-slate-800">
                      QA Email Recipients
                    </label>
                    <span className="text-[11px] font-semibold text-slate-500">
                      {recipients.length} configured
                    </span>
                  </div>
                  <p className="text-xs text-slate-500 mt-0.5">
                    Email notifications and reports will be dispatched to these addresses upon scheduled test execution.
                  </p>
                </div>

                {/* Add Email Recipient Form */}
                <div>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <div className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3">
                        <svg className="h-4 w-4 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
                        </svg>
                      </div>
                      <input
                        ref={emailInputRef}
                        id="recipient-email-input"
                        type="email"
                        value={emailInput}
                        onChange={(e) => {
                          setEmailInput(e.target.value);
                          setEmailInputError(null);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            handleAddRecipient();
                          }
                        }}
                        placeholder="Enter email address (e.g. qa-lead@example.com)"
                        className="w-full rounded-lg border border-slate-300 bg-white pl-9 pr-3.5 py-2 text-xs text-slate-900 placeholder-slate-400 shadow-2xs focus:border-slate-800 focus:outline-none focus:ring-1 focus:ring-slate-800"
                      />
                    </div>
                    <button
                      type="button"
                      id="add-recipient-btn"
                      onClick={handleAddRecipient}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3.5 py-2 text-xs font-bold text-slate-700 shadow-xs hover:border-sky-300 hover:bg-sky-50 hover:text-sky-700 active:bg-sky-100 transition cursor-pointer"
                    >
                      <svg className="h-3.5 w-3.5 text-slate-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
                      </svg>
                      Add
                    </button>
                  </div>

                  {emailInputError && (
                    <p className="mt-1.5 text-xs font-semibold text-rose-600 flex items-center gap-1">
                      <svg className="h-3.5 w-3.5 text-rose-600 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                      </svg>
                      {emailInputError}
                    </p>
                  )}
                </div>

                {/* Recipient Chips List */}
                <div className="space-y-2">
                  {recipients.length === 0 ? (
                    <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50/50 p-4 text-center">
                      <p className="text-xs text-slate-500">
                        No recipients added yet. At least one recipient email address is required before saving.
                      </p>
                    </div>
                  ) : (
                    <div className="max-h-48 overflow-y-auto divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white">
                      {recipients.map((email) => (
                        <div
                          key={email}
                          className="flex items-center justify-between px-3.5 py-2 hover:bg-slate-50 transition-colors"
                        >
                          <div className="flex items-center gap-2 min-w-0">
                            <svg className="h-3.5 w-3.5 text-slate-400 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M16 12a4 4 0 10-8 0 4 4 0 008 0zm0 0v1.5a2.5 2.5 0 005 0V12a9 9 0 10-9 9m4.5-1.206a8.959 8.959 0 01-4.5 1.207" />
                            </svg>
                            <span className="truncate text-xs font-semibold text-slate-800" title={email}>
                              {email}
                            </span>
                          </div>

                          <button
                            type="button"
                            onClick={() => handleRemoveRecipient(email)}
                            className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold text-rose-600 hover:bg-rose-50 hover:text-rose-800 active:bg-rose-100 transition cursor-pointer"
                            title={`Remove ${email}`}
                            aria-label={`Remove recipient ${email}`}
                          >
                            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
                              <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                            </svg>
                            Remove
                          </button>
                        </div>
                      ))}
                    </div>
                  )}

                  {fieldErrors.recipients && (
                    <div className="space-y-0.5 mt-1">
                      {fieldErrors.recipients.map((err, i) => (
                        <p key={i} className="text-xs font-semibold text-rose-600 flex items-center gap-1">
                          <svg className="h-3.5 w-3.5 text-rose-600 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                          </svg>
                          {err}
                        </p>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </form>
          )}
        </div>

        {/* Modal Footer */}
        <div className="flex flex-col-reverse gap-3 border-t border-slate-200 bg-white px-6 py-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-[11px] text-slate-500">
            * Changes take effect immediately on backend execution schedules.
          </p>

          <div className="flex items-center justify-end gap-2.5">
            <button
              type="button"
              onClick={onClose}
              className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3.5 py-2 text-xs font-bold text-slate-700 shadow-2xs hover:bg-slate-50 active:bg-slate-100 transition cursor-pointer"
            >
              Cancel
            </button>

            <button
              type="submit"
              form="auto-rerun-form"
              disabled={isSaving || loading || Boolean(loadError)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-slate-900 px-4 py-2 text-xs font-bold text-white shadow-md hover:bg-slate-800 active:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50 transition cursor-pointer"
            >
              {isSaving ? (
                <>
                  <svg className="h-3.5 w-3.5 animate-spin text-white" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                  </svg>
                  <span>Saving Settings…</span>
                </>
              ) : (
                <>
                  <svg className="h-3.5 w-3.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2.5">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                  <span>Save Settings</span>
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
