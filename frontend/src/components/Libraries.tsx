import { Button } from "@/components/primitives/button";
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { bytes, mutate, number, request } from "../api";
import { useAction, useApp } from "../context";
import { refreshData, useResource } from "../hooks";
import { useT, type MessageKey } from "../i18n";
import type { Library } from "../types";
import { ChooseFolderButton, FolderChooser, type ChosenFolder } from "./FolderChooser";
import { Icon } from "./Icon";
import { ConfirmDialog, Dialog, Empty, ErrorNotice, Loading } from "./ui";

interface Health {
  unreadable: { name: string; path: string; reason: string }[];
  slowest_folders: { folder: string; files: number; seconds: number }[];
  average_mb_per_s: number | null;
}
interface ConnectionTest { online: boolean; reason?: string; latency_ms?: number; throughput_mb_per_s?: number | null }
interface RelinkReport { new_root: string; checked: number; matched: number; match_percent: number; applied: boolean }

const when = (value?: string | null) => value ? new Date(value.replace(" ", "T")).toLocaleString() : "";

function HealthPanel({ library }: { library: Library }) {
  const t = useT();
  const health = useResource<Health>(`/libraries/${library.id}/health`);
  const [test, setTest] = useState<ConnectionTest | null>(null);
  const action = useAction();
  return (
    <div className="library-health">
      <div className="inline-actions">
        <Button className="button small" disabled={action.busy} onClick={() => void action.run(async () => setTest(await mutate<ConnectionTest>(`/libraries/${library.id}/test`)), undefined, false)}>
          {action.busy ? t("lib.testing") : t("lib.test")}
        </Button>
        {test && <span className="small-text" role="status">{test.online
          ? t("lib.testResult", { latency: String(test.latency_ms ?? 0), speed: test.throughput_mb_per_s == null ? "–" : String(test.throughput_mb_per_s) })
          : t("lib.testOffline")}</span>}
      </div>
      <ErrorNotice error={health.error || action.error} retry={health.reload} />
      {health.data && (
        <>
          {health.data.average_mb_per_s != null && <p className="small-text muted">{t("lib.readSpeed", { speed: String(health.data.average_mb_per_s) })}</p>}
          <h4>{t("lib.unreadable", { count: number(health.data.unreadable.length) })}</h4>
          {health.data.unreadable.length === 0 ? <p className="small-text muted">{t("lib.noUnreadable")}</p> : (
            <ul className="library-problems">{health.data.unreadable.slice(0, 30).map((u) => <li key={u.path}><code>{u.path}</code><span className="small-text muted">{u.reason}</span></li>)}</ul>
          )}
          {health.data.slowest_folders.length > 0 && (
            <>
              <h4>{t("lib.slowest")}</h4>
              <ul className="library-problems">{health.data.slowest_folders.slice(0, 5).map((f) => (
                <li key={f.folder}><code>{f.folder || "/"}</code><span className="small-text muted">{t("lib.slowRow", { seconds: String(f.seconds), files: number(Math.max(0, f.files)) })}</span></li>))}</ul>
            </>
          )}
        </>
      )}
    </div>
  );
}

function LibraryCard({ library, reload }: { library: Library; reload: () => void }) {
  const t = useT();
  const action = useAction();
  const { job } = useApp();
  const [remove, setRemove] = useState(false);
  const [force, setForce] = useState(false);
  const [settings, setSettings] = useState(false);
  const [health, setHealth] = useState(false);
  const [exclude, setExclude] = useState(false);
  const [relink, setRelink] = useState(false);
  const [report, setReport] = useState<RelinkReport | null>(null);
  const active = !!job && ["running", "queued", "paused"].includes(job.status);
  const scanning = active && job?.library_id === library.id;
  const offline = library.status === "offline";
  const kind = library.kind ?? "local";
  const run = (fn: () => Promise<unknown>, done?: string) => void action.run(async () => { await fn(); reload(); refreshData(); }, done, false);
  const relative = (path: string) => path.startsWith(library.path) ? path.slice(library.path.length).replace(/^[\\/]+/, "").replace(/\\/g, "/") : null;
  const extra = scanning ? job?.extra : null;
  return (
    <article className={`library-card ${offline ? "offline" : ""}`} aria-label={library.name}>
      <div className="library-card-heading">
        <div className="folder-icon"><Icon name={kind === "network" ? "layers" : kind === "external" ? "drive" : "folder"} size={25} /></div>
        <div>
          <h3>{library.name}</h3>
          <p className="library-path">{library.share?.server ? `${library.share.protocol ?? "smb"}://${library.share.server}/${library.share.name ?? ""}${library.share.subpath ? `/${library.share.subpath}` : ""}` : library.path}</p>
        </div>
        <span className={`badge ${offline ? "amber" : "teal"}`}>{t(offline ? "lib.offline" : library.paused ? "lib.paused" : "lib.online")}</span>
      </div>
      <dl className="library-facts">
        <div><dt>{t("lib.type")}</dt><dd>{t(`lib.kind.${kind}` as MessageKey)}</dd></div>
        <div><dt>{t("lib.items")}</dt><dd>{number(library.media_count)}{library.video_count ? ` · ${t("lib.videos", { count: number(library.video_count) })}` : ""}</dd></div>
        <div><dt>{t("lib.size")}</dt><dd>{bytes(library.bytes ?? 0)}</dd></div>
        <div><dt>{t("lib.lastScan")}</dt><dd>{library.last_scan_at ? when(library.last_scan_at) : t("lib.never")}</dd></div>
        {library.next_scan_at && <div><dt>{t("lib.nextScan")}</dt><dd>{when(library.next_scan_at)}</dd></div>}
        {(library.failed_count ?? 0) > 0 && <div><dt>{t("lib.errors")}</dt><dd>{number(library.failed_count ?? 0)}</dd></div>}
      </dl>
      {offline && (
        <p className="library-offline-note" role="status">
          <Icon name="alert" size={16} />{t("lib.offlineNote")}
          <Button className="button small" disabled={action.busy} onClick={() => run(async () => {
            const r = await mutate<{ online: boolean; error?: { message: string; next_step: string } | null }>(`/libraries/${library.id}/reconnect`);
            if (!r.online) throw new Error(r.error ? `${r.error.message} ${r.error.next_step}` : t("lib.stillOffline"));
          }, t("lib.backOnline"))}>{t("lib.reconnect")}</Button>
        </p>
      )}
      {scanning && extra && (
        <p className="small-text muted" role="status">
          {t("lib.progress", { done: number(job?.processed ?? 0), total: number(job?.total ?? 0) })}
          {extra.mb_per_s ? ` · ${t("lib.speed", { speed: String(extra.mb_per_s) })}` : ""}
          {extra.eta_seconds ? ` · ${t("lib.eta", { minutes: String(Math.max(1, Math.round(extra.eta_seconds / 60))) })}` : ""}
        </p>
      )}
      {library.ignored.length > 0 && <p className="ignored-summary">{t("lib.excluded", { folders: library.ignored.join(", ") })}</p>}
      <ErrorNotice error={action.error} />
      <div className="library-actions">
        <Button className="button primary small" disabled={action.busy || active || offline} onClick={() => run(() => mutate(`/libraries/${library.id}/scan`), t("lib.scanStarted"))}>
          <Icon name="play" size={15} />{t("lib.scanNow")}
        </Button>
        <Button className="button small" disabled={action.busy} onClick={() => run(() => mutate(`/libraries/${library.id}`, { paused: !library.paused }, "PATCH"))}>
          {t(library.paused ? "lib.resume" : "lib.pause")}
        </Button>
        <Button className="button small" disabled={action.busy || offline} onClick={() => setExclude(true)}>{t("lib.editFolders")}</Button>
        <Button className="button small" disabled={action.busy || active} onClick={() => { setReport(null); setRelink(true); }}>{t("lib.relink")}</Button>
        <Button className="button ghost small" onClick={() => setSettings(true)}>{t("lib.settings")}</Button>
        <Button className="button ghost small" aria-expanded={health} onClick={() => setHealth((v) => !v)}>{t("lib.health")}</Button>
        <Button className="button ghost small" disabled={action.busy || active || offline} onClick={() => setForce(true)}>{t("lib.rescanAll")}</Button>
        <Button className="icon-button danger-text" aria-label={t("lib.removeLabel", { name: library.name })} disabled={active || action.busy} onClick={() => setRemove(true)}><Icon name="trash" size={18} /></Button>
      </div>
      {health && <HealthPanel library={library} />}
      <FolderChooser open={exclude} onClose={() => setExclude(false)} multiple startPath={library.path} title={t("lib.excludeTitle", { name: library.name })}
        confirmLabel={t("lib.excludeConfirm")}
        onChoose={async (folders) => {
          const inside = folders.map((f) => relative(f.path)).filter((p): p is string => !!p);
          if (!inside.length) throw new Error(t("lib.excludeOutside"));
          await mutate(`/libraries/${library.id}`, { ignored: [...new Set([...library.ignored, ...inside])] }, "PATCH");
          reload();
        }} />
      <FolderChooser open={relink && !report} onClose={() => setRelink(false)} title={t("lib.relinkTitle", { name: library.name })} confirmLabel={t("lib.relinkCheck")}
        onChoose={async (folders: ChosenFolder[]) => { setReport(await mutate<RelinkReport>(`/libraries/${library.id}/relink`, { path: folders[0].path })); setRelink(true); }} />
      <Dialog open={relink && !!report} onClose={() => { setRelink(false); setReport(null); }} title={t("lib.relinkTitle", { name: library.name })} busy={action.busy}>
        <div className="dialog-body">
          {report && (
            <>
              <p><code>{report.new_root}</code></p>
              <p role="status">{t("lib.relinkMatch", { matched: number(report.matched), checked: number(report.checked), percent: String(report.match_percent) })}</p>
              <p className="muted small-text">{t(report.match_percent >= 90 ? "lib.relinkGood" : "lib.relinkBad")}</p>
            </>
          )}
          <ErrorNotice error={action.error} />
        </div>
        <div className="dialog-footer">
          <Button className="button" disabled={action.busy} onClick={() => { setRelink(false); setReport(null); }}>{t("common.cancel")}</Button>
          <Button className="button primary" disabled={action.busy || !report} onClick={() => void action.run(async () => {
            await mutate(`/libraries/${library.id}/relink`, { path: report!.new_root, apply: true });
            setRelink(false); setReport(null); reload(); refreshData();
          }, t("lib.relinked"), false)}>{t("lib.relinkApply")}</Button>
        </div>
      </Dialog>
      <LibrarySettingsDialog open={settings} onClose={() => setSettings(false)} library={library} reload={reload} />
      <ConfirmDialog open={remove} onClose={() => setRemove(false)} title={t("lib.removeTitle", { name: library.name })} description={t("lib.removeDescription")}
        label={t("lib.remove")} confirmation="REMOVE LIBRARY" onConfirm={() => mutate(`/libraries/${library.id}`, { confirm: "REMOVE LIBRARY" }, "DELETE")}
        success={t("lib.removed")} />
      <ConfirmDialog open={force} onClose={() => setForce(false)} title={t("lib.rescanTitle", { name: library.name })} description={t("lib.rescanDescription")}
        label={t("lib.rescanAll")} danger={false} onConfirm={() => mutate(`/libraries/${library.id}/scan`, { force: true })} success={t("lib.scanStarted")} />
    </article>
  );
}

function LibrarySettingsDialog({ open, onClose, library, reload }: { open: boolean; onClose: () => void; library: Library; reload: () => void }) {
  const t = useT();
  const action = useAction();
  const s = library.settings ?? { scan_videos: true, poll_minutes: 15, concurrency: 4, pause_on_battery: false };
  const [name, setName] = useState(library.name);
  const [videos, setVideos] = useState(s.scan_videos);
  const [poll, setPoll] = useState(String(s.poll_minutes));
  const [readers, setReaders] = useState(String(s.concurrency));
  const [battery, setBattery] = useState(s.pause_on_battery);
  const [ignored, setIgnored] = useState(library.ignored);
  useEffect(() => { if (open) { setName(library.name); setVideos(s.scan_videos); setPoll(String(s.poll_minutes)); setReaders(String(s.concurrency)); setBattery(s.pause_on_battery); setIgnored(library.ignored); } },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [open]);
  return (
    <Dialog open={open} onClose={onClose} title={t("lib.settingsTitle", { name: library.name })} busy={action.busy}>
      <form onSubmit={(event) => {
        event.preventDefault();
        void action.run(async () => {
          await mutate(`/libraries/${library.id}`, { name, scan_videos: videos, poll_minutes: Number(poll) || 15, concurrency: Number(readers) || 4, pause_on_battery: battery, ignored }, "PATCH");
          reload(); onClose();
        }, t("lib.saved"), false);
      }}>
        <div className="dialog-body library-settings">
          <label className="field">{t("lib.name")}<input value={name} onChange={(e) => setName(e.target.value)} maxLength={120} required /></label>
          <label className="check-label"><input type="checkbox" checked={videos} onChange={(e) => setVideos(e.target.checked)} />{t("lib.scanVideos")}</label>
          {library.kind === "network" && (
            <>
              <label className="field">{t("lib.pollMinutes")}<input type="number" min={1} max={1440} value={poll} onChange={(e) => setPoll(e.target.value)} /></label>
              <label className="field">{t("lib.readers")}<input type="number" min={1} max={16} value={readers} onChange={(e) => setReaders(e.target.value)} /></label>
              <label className="check-label"><input type="checkbox" checked={battery} onChange={(e) => setBattery(e.target.checked)} />{t("lib.pauseOnBattery")}</label>
            </>
          )}
          <fieldset className="library-excluded">
            <legend>{t("lib.excludedFolders")}</legend>
            {ignored.length === 0 ? <p className="small-text muted">{t("lib.noneExcluded")}</p> : (
              <ul>{ignored.map((folder) => (
                <li key={folder}><code>{folder}</code><Button type="button" className="button small ghost" onClick={() => setIgnored((list) => list.filter((f) => f !== folder))}>{t("lib.include")}</Button></li>))}</ul>
            )}
          </fieldset>
          <ErrorNotice error={action.error} />
        </div>
        <div className="dialog-footer">
          <Button type="button" className="button" disabled={action.busy} onClick={onClose}>{t("common.cancel")}</Button>
          <Button className="button primary" disabled={action.busy}>{t("lib.save")}</Button>
        </div>
      </form>
    </Dialog>
  );
}

/** Every library with where it lives, whether it is reachable, and what can be done with it. */
export function Libraries() {
  const t = useT();
  const resource = useResource<{ items: Library[] }>("/libraries");
  const action = useAction();
  const { job } = useApp();
  // While a scan runs (or a library is offline) keep the cards fresh.
  const busy = !!job && ["running", "queued", "paused"].includes(job.status);
  const anyOffline = !!resource.data?.items.some((l) => l.status === "offline");
  useEffect(() => {
    if (!busy && !anyOffline) return;
    const timer = setInterval(() => resource.reload(), busy ? 3000 : 15000);
    return () => clearInterval(timer);
  }, [busy, anyOffline, resource]);
  const add = async (folders: ChosenFolder[]) => {
    for (const folder of folders) await mutate("/libraries", { path: folder.path });
    resource.reload();
    refreshData();
  };
  return (
    <section className="settings-section" id="libraries" aria-labelledby="libraries-heading">
      <div className="section-heading">
        <div><p className="eyebrow">{t("lib.eyebrow")}</p><h2 id="libraries-heading">{t("lib.title")}</h2></div>
        <ChooseFolderButton className="button primary small" label={t("lib.add")} multiple onChoose={(folders) => action.run(() => add(folders), t("lib.added"), false).then(() => undefined)} />
      </div>
      <p className="muted">{t("lib.description")}</p>
      <ErrorNotice error={resource.error || action.error} retry={resource.reload} />
      {resource.loading && !resource.data ? <Loading label={t("common.loading")} /> : resource.data?.items.length ? (
        <div className="libraries-list">{resource.data.items.map((library) => <LibraryCard library={library} key={library.id} reload={resource.reload} />)}</div>
      ) : resource.data && (
        <Empty icon="folder" title={t("lib.emptyTitle")} description={t("lib.emptyDescription")}>
          <ChooseFolderButton label={t("lib.chooseFirst")} multiple onChoose={(folders) => action.run(() => add(folders), t("lib.added"), false).then(() => undefined)} />
        </Empty>
      )}
    </section>
  );
}

/** Shown on every screen while a library cannot be reached: nothing is lost, and one click retries. */
export function OfflineBanner() {
  const t = useT();
  const libraries = useResource<{ items: Library[] }>("/libraries");
  const [busy, setBusy] = useState(false);
  const offline = (libraries.data?.items ?? []).filter((l) => l.status === "offline");
  const { pathname } = useLocation();
  // Check again whenever the user moves to another screen, and on a timer in between.
  useEffect(() => { libraries.reload(); },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pathname]);
  useEffect(() => {
    const timer = setInterval(() => libraries.reload(), offline.length ? 15000 : 60000);
    return () => clearInterval(timer);
  }, [offline.length, libraries]);
  if (!offline.length) return null;
  const reconnect = async () => {
    setBusy(true);
    try {
      for (const library of offline) await mutate(`/libraries/${library.id}/reconnect`).catch(() => undefined);
    } finally {
      setBusy(false);
      libraries.reload();
      refreshData();
    }
  };
  return (
    <div className="offline-banner" role="status">
      <Icon name="alert" size={18} />
      <span>{offline.length === 1 ? t("lib.bannerOne", { name: offline[0].name }) : t("lib.bannerMany", { count: number(offline.length) })}</span>
      <Button className="button small" disabled={busy} onClick={() => void reconnect()}>{busy ? t("lib.reconnecting") : t("lib.reconnect")}</Button>
    </div>
  );
}
void request;
