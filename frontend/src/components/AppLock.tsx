import { Button } from "@/components/primitives/button";
import { useEffect, useState, type ReactNode } from "react";
import { mutate, request } from "../api";
import { useAction } from "../context";
import { useResource } from "../hooks";
import { useT } from "../i18n";
import { ErrorNotice, Loading } from "./ui";
import { FaceCamera, AdminFaceSettings } from "./FaceUnlock";

interface LockStatus { enabled: boolean; unlocked: boolean; managed_by_environment: boolean; face_enabled?: boolean }

/** Shows the unlock form instead of the app while the optional app lock is closed. */
export function LockGate({ children }: { children: ReactNode }) {
  const t = useT();
  const [locked, setLocked] = useState(true);
  const [checking, setChecking] = useState(true);
  const [faceEnabled, setFaceEnabled] = useState(false);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const onLocked = () => setLocked(true);
    window.addEventListener("lfs-locked", onLocked);
    void request<LockStatus>("/lock").then(s => {
      setLocked(s.enabled && !s.unlocked); setFaceEnabled(Boolean(s.face_enabled));
    }).catch(() => { setLocked(true); setError(t('faceLock.checkError')); })
      .finally(() => setChecking(false));
    return () => window.removeEventListener("lfs-locked", onLocked);
  }, []);
  if (checking) return <main className="lock-screen"><Loading label={t("faceLock.checking")} /></main>;
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
        {faceEnabled && <FaceCamera onCapture={async images => {
          const attempt = await mutate<{ challenge: string }>('/lock/face/challenge', {});
          await mutate('/lock/face/unlock', { challenge: attempt.challenge, image: images[0] });
          window.location.reload();
        }} />}
        <p className="muted">{faceEnabled ? t('faceLock.recovery') : t("lock.prompt")}</p>
        <label htmlFor="lock-password">{t("lock.password")}</label>
        <input id="lock-password" type="password" autoComplete="current-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
        <ErrorNotice error={error} />
        <Button className="button primary" disabled={busy || !password}>{t("lock.unlock")}</Button>
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
            <Button className="button small primary" disabled={action.busy || next.length < 8 || next !== again || (s.enabled && !current)}>
              {s.enabled ? t("lock.change") : t("lock.set")}</Button>
            {s.enabled && <Button type="button" className="button small danger-text" disabled={action.busy || !current}
              onClick={() => void action.run(async () => { await mutate("/lock/password", { password: current }, "DELETE"); done(); }, t("lock.removed"), false)}>{t("lock.remove")}</Button>}
            {s.enabled && <Button type="button" className="button small" disabled={action.busy}
              onClick={() => void mutate("/lock/lock").then(() => window.location.reload())}>{t("lock.lockNow")}</Button>}
          </div>
        </form>
      )}
      {s && <AdminFaceSettings enabled={s.enabled} faceEnabled={Boolean(s.face_enabled)} onDone={done} />}
    </section>
  );
}
