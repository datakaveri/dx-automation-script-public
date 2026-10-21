import { useEffect, useState } from "react";
import { createPortal } from "react-dom";

export type ToastType = "success" | "error" | "info";

export interface ToastItem {
  id: string;
  type: ToastType;
  message: string;
  duration?: number;
}

type ToastListener = (toasts: ToastItem[]) => void;

let activeToasts: ToastItem[] = [];
const toastListeners = new Set<ToastListener>();
const toastTimers = new Map<string, ReturnType<typeof setTimeout>>();

function notify() {
  const current = [...activeToasts];
  toastListeners.forEach((listener) => listener(current));
}

export const toast = {
  show: (type: ToastType, message: string, duration = 4000): string => {
    const id = Math.random().toString(36).slice(2, 9);
    const item: ToastItem = { id, type, message, duration };
    activeToasts = [...activeToasts, item];
    notify();

    if (duration > 0) {
      const timer = setTimeout(() => {
        toast.dismiss(id);
      }, duration);
      toastTimers.set(id, timer);
    }
    return id;
  },

  success: (message: string, duration = 4000): string => {
    return toast.show("success", message, duration);
  },

  error: (message: string, duration = 4000): string => {
    return toast.show("error", message, duration);
  },

  info: (message: string, duration = 4000): string => {
    return toast.show("info", message, duration);
  },

  dismiss: (id: string): void => {
    const timer = toastTimers.get(id);
    if (timer) {
      clearTimeout(timer);
      toastTimers.delete(id);
    }
    activeToasts = activeToasts.filter((t) => t.id !== id);
    notify();
  },

  clear: (): void => {
    toastTimers.forEach((timer) => clearTimeout(timer));
    toastTimers.clear();
    activeToasts = [];
    notify();
  },
};

export function useToasts() {
  const [toasts, setToasts] = useState<ToastItem[]>(activeToasts);

  useEffect(() => {
    const handleChange = (items: ToastItem[]) => setToasts(items);
    toastListeners.add(handleChange);
    return () => {
      toastListeners.delete(handleChange);
    };
  }, []);

  return { toasts, dismiss: toast.dismiss };
}

export function ToastContainer() {
  const { toasts, dismiss } = useToasts();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted || typeof document === "undefined" || toasts.length === 0) {
    return null;
  }

  return createPortal(
    <div
      className="fixed bottom-5 right-5 z-[9999] flex flex-col gap-2.5 pointer-events-none max-w-sm w-full px-4 sm:px-0"
      aria-live="polite"
      id="toast-container"
    >
      {toasts.map((t) => {
        const isSuccess = t.type === "success";
        const isError = t.type === "error";

        return (
          <div
            key={t.id}
            role={isError ? "alert" : "status"}
            aria-atomic="true"
            className={`pointer-events-auto flex items-start gap-3 rounded-xl border p-4 shadow-xl transition-all duration-200 ${
              isSuccess
                ? "border-emerald-300 bg-emerald-50 text-emerald-950 ring-1 ring-emerald-500/15"
                : isError
                ? "border-rose-300 bg-rose-50 text-rose-950 ring-1 ring-rose-500/15"
                : "border-sky-300 bg-sky-50 text-sky-950 ring-1 ring-sky-500/15"
            }`}
          >
            {isSuccess && (
              <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-emerald-200/80 text-emerald-700">
                <svg
                  className="h-4 w-4"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  aria-hidden="true"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M5 13l4 4L19 7"
                  />
                </svg>
              </div>
            )}

            {isError && (
              <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-rose-200/80 text-rose-700">
                <svg
                  className="h-4 w-4"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth="2"
                  aria-hidden="true"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                  />
                </svg>
              </div>
            )}

            {!isSuccess && !isError && (
              <div className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-sky-200/80 text-sky-700">
                <svg
                  className="h-4 w-4"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  strokeWidth="2"
                  aria-hidden="true"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
                  />
                </svg>
              </div>
            )}

            <div className="flex-1 min-w-0 pt-0.5">
              <p className="text-xs font-semibold leading-relaxed break-words">
                {t.message}
              </p>
            </div>

            <button
              type="button"
              onClick={() => dismiss(t.id)}
              aria-label="Dismiss notification"
              className={`shrink-0 rounded-md p-1 transition-colors cursor-pointer ${
                isSuccess
                  ? "text-emerald-700 hover:text-emerald-950 hover:bg-emerald-200/50"
                  : isError
                  ? "text-rose-700 hover:text-rose-950 hover:bg-rose-200/50"
                  : "text-sky-700 hover:text-sky-950 hover:bg-sky-200/50"
              }`}
            >
              <svg
                className="h-4 w-4"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth="2"
                aria-hidden="true"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </button>
          </div>
        );
      })}
    </div>,
    document.body
  );
}
