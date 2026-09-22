import { createContext, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { request } from "./api";
import { refreshData, useMounted } from "./hooks";
import type { Job, Settings, Theme } from "./types";

interface Notice {
  id: number;
  text: string;
  error: boolean;
}
interface AppState {
  job: Job | null;
  jobError: string;
  theme: Theme;
  setTheme: (value: Theme) => void;
  notify: (text: string, error?: boolean) => void;
}
const AppContext = createContext<AppState | null>(null);
export function AppProvider({ children }: { children: ReactNode }) {
  const [job, setJob] = useState<Job | null>(null);
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
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const notify = (text: string, error = false) => {
    const id = ++noticeId.current;
    setNotices((current) => [...current.slice(-3), { id, text, error }]);
    if (!error)
      timers.current.push(
        setTimeout(
          () =>
            setNotices((current) => current.filter((item) => item.id !== id)),
          6000,
        ),
      );
  };
  useEffect(() => () => timers.current.forEach(clearTimeout), []);
  const setTheme = (value: Theme) => {
    updateTheme(value);
    try {
      localStorage.setItem("lfs-theme", value);
    } catch {
      /* Storage is optional. */
    }
  };
  useEffect(() => {
    const controller = new AbortController();
    void request<Settings>("/settings", { signal: controller.signal })
      .then((data) => setTheme(data.theme))
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
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let previous: Job | null = null;
    const poll = async () => {
      try {
        const next = await request<Job | null>("/index/status", {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setJob(next);
        setJobError("");
        if (previous && next && previous.status !== next.status) refreshData();
        previous = next;
      } catch (error) {
        if (controller.signal.aborted) return;
        setJobError(error instanceof Error ? error.message : String(error));
      }
      timer = setTimeout(poll, 2000);
    };
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, []);
  return (
    <AppContext.Provider value={{ job, jobError, theme, setTheme, notify }}>
      {children}
      <div className="toast-stack" role="region" aria-label="Notifications">
        {notices.map((item) => (
          <div
            className={`toast ${item.error ? "toast-error" : ""}`}
            key={item.id}
            role={item.error ? "alert" : "status"}
          >
            <span>{item.text}</span>
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
  );
}
export function useApp() {
  const value = useContext(AppContext);
  if (!value) throw new Error("AppProvider is missing.");
  return value;
}
export function useAction() {
  const { notify } = useApp();
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
