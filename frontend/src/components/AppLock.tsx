import { useEffect, useState, type ReactNode } from "react";
import { mutate, request } from "../api";
import { useAction } from "../context";
import { useResource } from "../hooks";
import { useT } from "../i18n";
import { ErrorNotice } from "./ui";

interface LockStatus { enabled: boolean; unlocked: boolean; managed_by_environment: boolean }

/** Shows the unlock form instead of the app while the optional app lock is closed. */
export function LockGate({ children }: { children: ReactNode }) {
  const t = useT();
  const [locked, setLocked] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const onLocked = () => setLocked(true);
    window.addEventListener("lfs-locked", onLocked);
    void request<LockStatus>("/lock").then((s) => { if (s.enabled && !s.unlocked) setLocked(true); }).catch(() => {});
    return () => window.removeEventListener("lfs-locked", onLocked);
  }, []);
  if (!locked) return <>{children}</>;
  return (
    <main className="lock-screen">
      <form className="lock-card" onSubmit={(e) => {
        e.preventDefault();
        setBusy(true);
        setError("");
        mutate("/lock/unlock", { password }).then(() => window.location.reload())
          .catch((err: unknown) => { setError(err instanceof Error ? err.message : String(err)); setBusy(false); });
      }}>
        <h1>{t("lock.title")}</h1>
        <p className="muted">{t("lock.prompt")}</p>
        <label htmlFor="lock-password">{t("lock.password")}</label>
        <input id="lock-password" type="password" autoComplete="current-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
        <ErrorNotice error={error} />
        <button className="button primary" disabled={busy || !password}>{t("lock.unlock")}</button>
      </form>
    </main>
  );
}

/** Settings card: set, change or remove the app password. */
export function AppLockCard() {
  const t = useT();
  const status = useResource<LockStatus>("/lock");
  const action = useAction();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const s = status.data;
  const done = () => { setCurrent(""); setNext(""); setAgain(""); status.reload(); };
  return (
    <section className="settings-section" aria-labelledby="lock-heading">
      <div className="section-heading"><div><h2 id="lock-heading">{t("lock.heading")}</h2><p>{t("lock.help")}</p></div></div>
      <ErrorNotice error={status.error || action.error} retry={status.reload} />
      {s?.managed_by_environment ? <p className="small-text muted">{t("lock.managed")}</p> : s && (
        <form className="lock-form" onSubmit={(e) => {
          e.preventDefault();
          void action.run(async () => { await mutate("/lock/password", { password: next, current: s.enabled ? current : undefined }, "PUT"); done(); }, t("lock.saved"), false);
        }}>
          <p role="status" className="small-text">{s.enabled ? t("lock.isOn") : t("lock.isOff")}</p>
          {s.enabled && <label>{t("lock.current")}<input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} /></label>}
          <label>{t("lock.new")}<input type="password" autoComplete="new-password" minLength={8} value={next} onChange={(e) => setNext(e.target.value)} /></label>
          <label>{t("lock.repeat")}<input type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} /></label>
          <div className="inline-actions">
            <button className="button small primary" disabled={action.busy || next.length < 8 || next !== again || (s.enabled && !current)}>
              {s.enabled ? t("lock.change") : t("lock.set")}</button>
            {s.enabled && <button type="button" className="button small danger-text" disabled={action.busy || !current}
              onClick={() => void action.run(async () => { await mutate("/lock/password", { password: current }, "DELETE"); done(); }, t("lock.removed"), false)}>{t("lock.remove")}</button>}
            {s.enabled && <button type="button" className="button small" disabled={action.busy}
              onClick={() => void mutate("/lock/lock").then(() => window.location.reload())}>{t("lock.lockNow")}</button>}
          </div>
        </form>
      )}
    </section>
  );
}
