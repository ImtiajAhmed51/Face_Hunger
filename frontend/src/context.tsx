import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { request } from "./api";
import { refreshData, useMounted } from "./hooks";
import { queryClient } from "./queryClient";
import type { Job, Settings, Theme } from "./types";
import { UndoStack } from "./undo";

interface Notice {
  id: number;
  text: string;
  error: boolean;
  undoId?: number;
}
interface AppState {
  job: Job | null;
  /** Every active or recent job (queue + legacy scans), live via SSE. */
  jobs: Job[];
  jobError: string;
  theme: Theme;
  setTheme: (value: Theme) => void;
  notify: (text: string, error?: boolean) => void;
  /** Record an undoable action; shows a toast with Undo, and Ctrl/Cmd+Z runs the latest. */
  pushUndo: (label: string, undo: () => Promise<unknown> | unknown, options?: { silent?: boolean }) => void;
  undoLast: () => Promise<void>;
  canUndo: boolean;
}
export const undoStack = new UndoStack();
const AppContext = createContext<AppState | null>(null);
const NotifyContext = createContext<AppState['notify'] | null>(null);
export function AppProvider({ children }: { children: ReactNode }) {
  const [job, setJob] = useState<Job | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [canUndo, setCanUndo] = useState(false);
  const [jobError, setJobError] = useState("");
  const [theme, updateTheme] = useState<Theme>(() => {
    try {
      const value = localStorage.getItem("lfs-theme");
      return value === "light" || value === "dark" ? value : "system";
    } catch {
      return "system";
    }
  });
  const [notices, setNotices] = useState<Notice[]>([]);
  const noticeId = useRef(0);
  const timers = useRef(new Set<ReturnType<typeof setTimeout>>());
  const notify = useCallback((text: string, error = false) => {
    const id = ++noticeId.current;
    setNotices(current => [...current.slice(-3), { id, text, error }]);
    if (!error) {
      const timer = setTimeout(() => {
        timers.current.delete(timer);
        setNotices(current => current.filter(item => item.id !== id));
      }, 6000);
      timers.current.add(timer);
    }
  }, []);
  useEffect(() => () => { timers.current.forEach(clearTimeout); timers.current.clear(); }, []);
  useEffect(() => undoStack.subscribe(() => setCanUndo(undoStack.size > 0)), []);
  const pushUndo = useCallback((label: string, undo: () => Promise<unknown> | unknown, options?: { silent?: boolean }) => {
    const entry = undoStack.push(label, undo);
    if (options?.silent) return;
    const id = ++noticeId.current;
    setNotices(current => [...current.slice(-3), { id, text: label, error: false, undoId: entry.id }]);
    const timer = setTimeout(() => {
      timers.current.delete(timer);
      setNotices(current => current.filter(item => item.id !== id));
    }, 10000);
    timers.current.add(timer);
  }, []);
  const runUndo = useCallback(async (undoId?: number) => {
    try {
      const label = await undoStack.undo(undoId);
      if (label) {
        setNotices(current => current.filter(item => item.undoId !== undoId || undoId === undefined));
        notify(`Undone: ${label}`);
      }
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error), true);
    } finally {
      refreshData();
    }
  }, [notify]);
  const undoLast = useCallback(() => runUndo(), [runUndo]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== "z" || !(event.metaKey || event.ctrlKey) || event.shiftKey || event.altKey) return;
      if (event.target instanceof HTMLElement && event.target.closest("input,textarea,select,[contenteditable=true]")) return;
      if (!undoStack.size) return;
      event.preventDefault();
      void runUndo();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [runUndo]);
  const setTheme = useCallback((value: Theme) => {
    updateTheme(value);
    try {
      localStorage.setItem("lfs-theme", value);
    } catch {
      /* Storage is optional. */
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void request<Settings>("/settings", { signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setTheme(data.theme); })
      .catch(() => {});
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.dataset.theme =
        theme === "system" ? (media.matches ? "dark" : "light") : theme;
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);
  useEffect(() => {
    // Live job progress over SSE; falls back to polling where EventSource is unavailable.
    const known = new Map<number, Job>();
    const lastStatus = new Map<number, string>();
    const FINISHED = new Set(["completed", "failed", "cancelled", "interrupted"]);
    const publish = () => {
      const all = [...known.values()].sort((a, b) => b.id - a.id);
      setJobs(all);
      const latestIndex = all.find(item => (item.kind ?? "index") === "index") ?? null;
      setJob(current => JSON.stringify(current) === JSON.stringify(latestIndex) ? current : latestIndex);
      // New data appears when a job finishes: refresh every visible query once.
      const finished = all.some(item => FINISHED.has(item.status) && lastStatus.has(item.id) && !FINISHED.has(lastStatus.get(item.id)!));
      all.forEach(item => lastStatus.set(item.id, item.status));
      if (finished) refreshData();
    };
    if (typeof EventSource !== "undefined") {
      const source = new EventSource("/api/jobs/events");
      let frame = 0;
      source.addEventListener("job", (event) => {
        const next = JSON.parse((event as MessageEvent).data) as Job;
        known.set(next.id, next);
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(publish);
        setJobError("");
      });
      source.onerror = () => setJobError(source.readyState === EventSource.CLOSED ? "Lost connection to the local server." : "");
      // Close before the page unloads: an open stream cut by navigation logs a network error.
      const close = () => source.close();
      window.addEventListener("pagehide", close);
      window.addEventListener("beforeunload", close);
      return () => {
        cancelAnimationFrame(frame);
        window.removeEventListener("pagehide", close);
        window.removeEventListener("beforeunload", close);
        source.close();
      };
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      if (controller.signal.aborted) return;
      try {
        const next = await request<Job | null>("/index/status", { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (next) known.set(next.id, next);
        publish();
        setJobError("");
      } catch (error) {
        if (controller.signal.aborted) return;
        setJobError(error instanceof Error ? error.message : String(error));
      }
      timer = setTimeout(poll, 4000);
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, []);
  const value = useMemo(() => ({ job, jobs, jobError, theme, setTheme, notify, pushUndo, undoLast, canUndo }),
    [job, jobs, jobError, theme, setTheme, notify, pushUndo, undoLast, canUndo]);
  return (
    <QueryClientProvider client={queryClient}>
    <AppContext.Provider value={value}>
      <NotifyContext.Provider value={notify}>{children}</NotifyContext.Provider>
      <div className="toast-stack" role="region" aria-label="Notifications">
        {notices.map((item) => (
          <div
            className={`toast ${item.error ? "toast-error" : ""}`}
            key={item.id}
            role={item.error ? "alert" : "status"}
          >
            <span>{item.text}</span>
            {item.undoId !== undefined && (
              <button className="button ghost small" onClick={() => void runUndo(item.undoId)}>
                Undo
              </button>
            )}
            <button
              className="icon-button"
              onClick={() =>
                setNotices((current) => current.filter((n) => n.id !== item.id))
              }
              aria-label="Dismiss notification"
            >
              &times;
            </button>
          </div>
        ))}
      </div>
    </AppContext.Provider>
    </QueryClientProvider>
  );
}
export function useApp() {
  const value = useContext(AppContext);
  if (!value) throw new Error("AppProvider is missing.");
  return value;
}
export function useAction() {
  const notify = useContext(NotifyContext);
  if (!notify) throw new Error('AppProvider is missing.');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lock = useRef(false);
  const mounted = useMounted();
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const run = useRef(
    async (
      action: () => Promise<unknown>,
      success?: string,
      refresh = true,
    ): Promise<boolean> => {
      if (lock.current) return false;
      lock.current = true;
      setBusy(true);
      setError("");
      try {
        await action();
        if (success) notifyRef.current(success);
        return true;
      } catch (cause) {
        const message = cause instanceof Error ? cause.message : String(cause);
        if (mounted.current) setError(message);
        notifyRef.current(message, true);
        return false;
      } finally {
        lock.current = false;
        if (mounted.current) setBusy(false);
        if (refresh) refreshData();
      }
    },
  );
  return { busy, error, run: run.current };
}
