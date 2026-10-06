import { useEffect, useMemo, useState } from "react";
import { bytes, mutate, number } from "../api";
import { undoAudit } from "../audit";
import { useAction, useApp } from "../context";
import { refreshData, useResource } from "../hooks";
import { useT, type MessageKey } from "../i18n";
import { Icon } from "../components/Icon";
import { MediaViewer } from "../components/MediaViewer";
import { Empty, ErrorNotice, Loading, PageHeader, Thumbnail } from "../components/ui";

interface Category { category: string; count: number; bytes: number; preview_ids: number[] }
interface Dashboard {
  library: { items: number; bytes: number; video_bytes: number };
  categories: Category[]; clutter: { count: number; bytes: number }; bin: { bytes: number; files: number };
}
interface Candidate { id: number; name: string; kind: string; size: number; reason: string }

export function Storage() {
  const t = useT();
  const dash = useResource<Dashboard>("/storage");
  const [category, setCategory] = useState<string | null>(null);
  const list = useResource<{ items: Candidate[] }>(category ? `/storage/${category}` : null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [free, setFree] = useState(true);
  const [viewer, setViewer] = useState<number | null>(null);
  const action = useAction();
  const { pushUndo } = useApp();
  const items = useMemo(() => list.data?.items ?? [], [list.data]);
  // Preview first: everything in a clutter category starts selected, "large files" start unselected.
  useEffect(() => { setSelected(new Set(category === "large" ? [] : items.map((i) => i.id))); }, [items, category]);
  const chosen = items.filter((i) => selected.has(i.id));
  const saving = chosen.reduce((n, i) => n + i.size, 0);

  return (
    <>
      <PageHeader eyebrow={t("storage.eyebrow")} title={t("storage.title")} description={t("storage.description")} />
      <ErrorNotice error={dash.error || list.error} retry={dash.reload} />
      {dash.loading ? <Loading label={t("common.loading")} /> : dash.data && (
        <>
          <p className="storage-total" role="status">
            {t("storage.total", { size: bytes(dash.data.library.bytes), items: number(dash.data.library.items) })}
            {" · "}{t("storage.clutter", { size: bytes(dash.data.clutter.bytes), count: number(dash.data.clutter.count) })}
          </p>
          <div className="storage-cards">
            {dash.data.categories.map((c) => (
              <button key={c.category} className={`storage-card ${category === c.category ? "active" : ""}`} aria-pressed={category === c.category}
                onClick={() => setCategory(c.category)} disabled={!c.count}>
                <strong>{t(`storage.cat.${c.category}` as MessageKey)}</strong>
                <span>{bytes(c.bytes)}</span>
                <span className="muted small-text">{t("library.items", { count: number(c.count) })}</span>
              </button>
            ))}
          </div>
        </>
      )}
      {category && (list.loading ? <Loading label={t("common.loading")} /> : !items.length ? (
        <Empty icon="cleanup" title={t("storage.none")} description={t("storage.noneDescription")} />
      ) : (
        <section className="collection" aria-label={t(`storage.cat.${category}` as MessageKey)}>
          <div className="selection-toolbar" role="region" aria-label={t("common.selectionActions")}>
            <strong>{t("storage.saving", { count: number(chosen.length), size: bytes(saving) })}</strong>
            <div className="inline-actions">
              <label className="check-label"><input type="checkbox" checked={free} onChange={(e) => setFree(e.target.checked)} />{t("storage.freeNow")}</label>
              <button className="button small" onClick={() => setSelected(new Set(selected.size ? [] : items.map((i) => i.id)))}>
                {selected.size ? t("common.clearSelection") : t("storage.selectAll")}
              </button>
              <button className="button small danger-text" disabled={!chosen.length || action.busy} onClick={() => void action.run(async () => {
                const r = await mutate<{ audit_id: number; removed: number }>("/storage/cleanup", { media_ids: chosen.map((i) => i.id), free_space: free });
                pushUndo(t("storage.cleaned", { count: number(r.removed) }), undoAudit(r.audit_id));
                refreshData();
              }, undefined, false)}>
                <Icon name="trash" size={16} />{t("storage.cleanup")}
              </button>
            </div>
          </div>
          <ul className="storage-grid">
            {items.map((item) => (
              <li key={item.id} className={selected.has(item.id) ? "selected" : ""}>
                <button className="storage-thumb" onClick={() => setViewer(item.id)} aria-label={t("storage.open", { name: item.name })}>
                  <Thumbnail src={`/api/media/${item.id}/thumbnail`} alt="" icon={item.kind === "video" ? "video" : "photo"} />
                </button>
                <label className="check-label">
                  <input type="checkbox" checked={selected.has(item.id)} onChange={(e) => setSelected((prev) => {
                    const next = new Set(prev);
                    if (e.target.checked) next.add(item.id); else next.delete(item.id);
                    return next;
                  })} />
                  <span className="storage-name">{item.name}</span>
                </label>
                <span className="muted small-text">{bytes(item.size)}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {viewer !== null && <MediaViewer id={viewer} ids={items.map((i) => i.id)} onClose={() => setViewer(null)} />}
    </>
  );
}

interface Option { key: string; role: string; dim: number; active: boolean; can_embed?: boolean; coverage: { filled: number; total: number } }
interface Upgrade {
  upgrade: null | { role: string; from_key: string | null; to_key: string; status: string; ready: boolean; active_key: string | null;
    coverage: { filled: number; total: number } };
  options: Option[];
}
interface Side { key: string; ids: number[] }

/** Build a new model's index beside the current one, compare, then switch or roll back. */
export function ModelUpgrade() {
  const t = useT();
  const status = useResource<Upgrade>("/models/upgrade");
  const action = useAction();
  const [compare, setCompare] = useState<{ current: Side; candidate: Side; overlap: number } | null>(null);
  const up = status.data?.upgrade;
  useEffect(() => {
    if (up?.status !== "building") return;
    const timer = setInterval(() => status.reload(), 2000);
    return () => clearInterval(timer);
  }, [up?.status, status]);
  const call = (path: string, body?: unknown) => void action.run(async () => { await mutate(path, body); status.reload(); }, undefined, false);
  const candidates = (status.data?.options ?? []).filter((o) => !o.active && o.can_embed !== false);
  return (
    <section className="settings-section" aria-labelledby="upgrade-heading">
      <div className="section-heading"><div><h2 id="upgrade-heading">{t("upgrade.title")}</h2><p>{t("upgrade.help")}</p></div></div>
      <ErrorNotice error={status.error || action.error} retry={status.reload} />
      {!up && (candidates.length === 0 ? <p className="muted small-text">{t("upgrade.none")}</p> : (
        <ul className="health-backups">
          {candidates.map((o) => (
            <li key={o.key}>
              <span>{o.key} <span className="muted small-text">· {o.role} · {number(o.coverage.filled)} / {number(o.coverage.total)}</span></span>
              <button className="button small" disabled={action.busy} onClick={() => call("/models/upgrade", { to_key: o.key })}>{t("upgrade.start")}</button>
            </li>
          ))}
        </ul>
      ))}
      {up && (
        <>
          <p role="status">{t(`upgrade.status.${up.status}` as MessageKey, { from: up.from_key ?? "–", to: up.to_key })}</p>
          <progress max={Math.max(1, up.coverage.total)} value={up.coverage.filled} aria-label={t("upgrade.progress")} />
          <span className="small-text muted"> {number(up.coverage.filled)} / {number(up.coverage.total)} · {t("upgrade.active", { key: up.active_key ?? "–" })}</span>
          <div className="inline-actions">
            {up.from_key && <button className="button small" disabled={action.busy || !up.coverage.filled} onClick={() => void action.run(async () => {
              const sample = await mutate<{ items: { id: number }[] }>("/search/hybrid", { limit: 20 });
              const pick = sample.items[Math.floor(Math.random() * Math.max(1, sample.items.length))];
              if (pick) setCompare(await mutate("/models/upgrade/compare", { similar_media_id: pick.id, limit: 6 }));
            }, undefined, false)}>{t("upgrade.compare")}</button>}
            <button className="button small primary" disabled={action.busy || !up.ready || up.active_key === up.to_key} onClick={() => call("/models/upgrade/switch")}>{t("upgrade.switch")}</button>
            {up.from_key && <button className="button small" disabled={action.busy || up.active_key === up.from_key} onClick={() => call("/models/upgrade/rollback")}>{t("upgrade.rollback")}</button>}
            <button className="button ghost small" disabled={action.busy || up.status === "building"} onClick={() => call("/models/upgrade/finish")}>{t("upgrade.finish")}</button>
          </div>
          {compare && (
            <div className="upgrade-compare" aria-label={t("upgrade.compare")}>
              {([["current", compare.current], ["candidate", compare.candidate]] as const).map(([label, side]) => (
                <div key={label}>
                  <span className="small-text muted">{t(`upgrade.side.${label}` as MessageKey)} · {side.key}</span>
                  <div className="memory-strip">{side.ids.map((id) => <span key={id} className="upgrade-thumb"><Thumbnail src={`/api/media/${id}/thumbnail`} alt="" /></span>)}</div>
                </div>
              ))}
              <span className="small-text muted">{t("upgrade.overlap", { count: compare.overlap })}</span>
            </div>
          )}
        </>
      )}
    </section>
  );
}
