import { useEffect, useRef, useState } from "react";
import { mutate, number } from "../api";
import { useAction, useApp } from "../context";
import { useResource } from "../hooks";
import { useT, type MessageKey } from "../i18n";
import { Empty, ErrorNotice, Loading, PageHeader } from "../components/ui";
import type { Job } from "../types";

interface Plugin {
  id: string; name: string; version: string; api_version: string; description: string; license: string; author: string;
  capabilities: Record<string, { title?: string; label?: string }>;
  requested_permissions: string[]; granted_permissions: string[]; enabled: boolean; last_error: string | null;
  process: null | { running: boolean; crashes: number; kernel_sandbox: boolean };
}
interface PluginList {
  api_version: string; kernel_sandbox: boolean; items: Plugin[];
  available: { id: string; name: string; version: string; path: string }[];
}

/** A plugin's own page, in a frame with no network and no access to the app. It can only post messages. */
function PluginPanel({ plugin }: { plugin: Plugin }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const title = plugin.capabilities.ui_panel?.title ?? plugin.name;
  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      const target = frame.current?.contentWindow;
      const data = event.data as { fh?: number; id?: number; method?: string } | null;
      if (!target || event.source !== target || !data || data.fh !== 1 || typeof data.method !== "string") return;
      // The server checks the method and the plugin's permissions; the frame's origin is opaque, hence "*".
      mutate(`/plugins/${plugin.id}/panel-rpc`, { method: data.method })
        .then((result) => target.postMessage({ fh: 1, id: data.id, ok: true, result }, "*"))
        .catch((error: unknown) => target.postMessage({ fh: 1, id: data.id, ok: false, error: error instanceof Error ? error.message : String(error) }, "*"));
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [plugin.id]);
  return <iframe ref={frame} className="plugin-panel" title={title} sandbox="allow-scripts" referrerPolicy="no-referrer"
    src={`/api/plugins/${plugin.id}/panel/`} />;
}

function ExportForm({ plugin }: { plugin: Plugin }) {
  const t = useT();
  const albums = useResource<{ items: { id: number; name: string }[] }>("/albums");
  const { jobs } = useApp();
  const action = useAction();
  const [album, setAlbum] = useState("");
  const [folder, setFolder] = useState("");
  const [jobId, setJobId] = useState<number | null>(null);
  const job = jobId === null ? undefined : jobs.find((j) => j.id === jobId);
  const result = (job?.progress ?? {}) as { written?: number; skipped?: number };
  return (
    <form className="plugin-export" onSubmit={(e) => {
      e.preventDefault();
      void action.run(async () => setJobId((await mutate<Job>(`/plugins/${plugin.id}/export`, { album_id: Number(album), target_dir: folder })).id), undefined, false);
    }}>
      <strong>{plugin.capabilities.export?.label ?? t("plugins.export")}</strong>
      <label>{t("plugins.exportAlbum")}
        <select value={album} onChange={(e) => setAlbum(e.target.value)} required>
          <option value="">{t("plugins.chooseAlbum")}</option>
          {(albums.data?.items ?? []).map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
      </label>
      <label>{t("plugins.exportFolder")}
        <input value={folder} onChange={(e) => setFolder(e.target.value)} required spellCheck={false} autoComplete="off" />
      </label>
      <button className="button small" disabled={action.busy || !album || !folder.trim()}>{t("plugins.runExport")}</button>
      <ErrorNotice error={action.error} />
      {job && <p role="status" className="small-text">
        {job.status === "completed" ? t("plugins.exported", { count: number(result.written ?? 0), skipped: number(result.skipped ?? 0) })
          : job.status === "failed" ? (job.error ?? "") : t("common.loading")}
      </p>}
    </form>
  );
}

function PluginCard({ plugin, reload }: { plugin: Plugin; reload: () => void }) {
  const t = useT();
  const action = useAction();
  const [granted, setGranted] = useState<string[]>(plugin.granted_permissions);
  useEffect(() => setGranted(plugin.granted_permissions), [plugin.granted_permissions]);
  const call = (fn: () => Promise<unknown>, done?: string) => void action.run(async () => { await fn(); reload(); }, done, false);
  const changed = granted.slice().sort().join() !== plugin.granted_permissions.slice().sort().join();
  return (
    <li className="plugin-card">
      <div className="plugin-head">
        <div>
          <h2>{plugin.name} <span className="muted small-text">{plugin.version}</span></h2>
          <p className="muted small-text">{plugin.description}</p>
          <p className="small-text">{Object.keys(plugin.capabilities).map((c) => t(`plugins.cap.${c}` as MessageKey)).join(" · ")}
            {plugin.license && ` · ${plugin.license}`}</p>
        </div>
        <label className="check-label">
          <input type="checkbox" checked={plugin.enabled} disabled={action.busy}
            onChange={(e) => call(() => mutate(`/plugins/${plugin.id}`, { enabled: e.target.checked, permissions: granted }, "PATCH"))} />
          {t("plugins.enabled")}
        </label>
      </div>
      <fieldset className="plugin-permissions">
        <legend>{t("plugins.permissions")}</legend>
        {plugin.requested_permissions.length === 0 ? <p className="small-text muted">{t("plugins.noPermissions")}</p>
          : plugin.requested_permissions.map((p) => (
            <label key={p} className="check-label">
              <input type="checkbox" checked={granted.includes(p)}
                onChange={(e) => setGranted((prev) => e.target.checked ? [...prev, p] : prev.filter((x) => x !== p))} />
              {t(`plugins.perm.${p}` as MessageKey)}
            </label>
          ))}
        {changed && <button className="button small primary" disabled={action.busy}
          onClick={() => call(() => mutate(`/plugins/${plugin.id}`, { permissions: granted }, "PATCH"), t("plugins.saved"))}>{t("plugins.savePermissions")}</button>}
      </fieldset>
      {plugin.last_error && <p className="small-text danger-text" role="status">{t("plugins.lastError", { error: plugin.last_error })}</p>}
      <ErrorNotice error={action.error} />
      {plugin.enabled && plugin.capabilities.export && <ExportForm plugin={plugin} />}
      {plugin.enabled && plugin.capabilities.embedding && <p className="small-text muted">{t("plugins.embeddingHint")}</p>}
      {plugin.enabled && plugin.capabilities.ui_panel && <PluginPanel key={plugin.granted_permissions.join()} plugin={plugin} />}
      <div className="inline-actions">
        {plugin.enabled && <button className="button small" disabled={action.busy}
          onClick={() => call(() => mutate(`/plugins/${plugin.id}/test`), t("plugins.testOk"))}>{t("plugins.test")}</button>}
        {plugin.enabled && plugin.capabilities.classifier && <button className="button small" disabled={action.busy}
          onClick={() => call(() => mutate(`/plugins/${plugin.id}/classify`), t("plugins.classifyStarted"))}>{t("plugins.classify")}</button>}
        <button className="button small danger-text" disabled={action.busy}
          onClick={() => call(() => mutate(`/plugins/${plugin.id}`, undefined, "DELETE"), t("plugins.uninstalled", { name: plugin.name }))}>{t("plugins.uninstall")}</button>
      </div>
    </li>
  );
}

export function Plugins() {
  const t = useT();
  const list = useResource<PluginList>("/plugins");
  const action = useAction();
  const [path, setPath] = useState("");
  const install = (folder: string) => void action.run(async () => { await mutate("/plugins/install", { path: folder }); setPath(""); list.reload(); }, t("plugins.installed"), false);
  return (
    <>
      <PageHeader eyebrow={t("plugins.eyebrow")} title={t("plugins.title")} description={t("plugins.description")} />
      <form className="plugin-install" onSubmit={(e) => { e.preventDefault(); install(path.trim()); }}>
        <label htmlFor="plugin-path">{t("plugins.installFrom")}</label>
        <input id="plugin-path" value={path} onChange={(e) => setPath(e.target.value)} spellCheck={false} autoComplete="off" />
        <button className="button primary" disabled={action.busy || !path.trim()}>{t("plugins.install")}</button>
      </form>
      <ErrorNotice error={list.error || action.error} retry={list.reload} />
      {list.data && <p className="small-text muted" role="status">
        {t("plugins.api", { version: list.data.api_version })} · {t(list.data.kernel_sandbox ? "plugins.sandboxKernel" : "plugins.sandboxBasic")}
      </p>}
      {(list.data?.available ?? []).map((a) => (
        <p key={a.id} className="small-text">{t("plugins.available", { name: a.name, version: a.version })}{" "}
          <button className="button small" disabled={action.busy} onClick={() => install(a.path)}>{t("plugins.install")}</button></p>
      ))}
      {list.loading ? <Loading label={t("common.loading")} /> : !list.data?.items.length ? (
        <Empty icon="settings" title={t("plugins.none")} description={t("plugins.noneDescription")} />
      ) : (
        <ul className="plugin-list">{list.data.items.map((p) => <PluginCard key={p.id} plugin={p} reload={list.reload} />)}</ul>
      )}
    </>
  );
}
