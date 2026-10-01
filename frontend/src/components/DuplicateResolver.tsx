import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent, type WheelEvent } from "react";
import { bytes, dateLabel, mutate, number } from "../api";
import { undoAudit } from "../audit";
import { useAction, useApp } from "../context";
import { refreshData, useResource } from "../hooks";
import { useT } from "../i18n";
import { ConfirmDialog, Dialog, ErrorNotice } from "./ui";

export interface ResolverItem { id: number; name: string; size: number; width: number | null; height: number | null; captured_at: string | null; path?: string; kind: string }
export interface ResolverGroup {
  type: string; key: string; similarity?: number | null; items: ResolverItem[];
  suggestion: { keep: number[]; remove: number[]; savings_bytes: number; reason: string; scores: Record<string, number> };
}

type Decision = Record<number, "keep" | "remove">;

function initial(groups: ResolverGroup[]): Record<string, Decision> {
  return Object.fromEntries(groups.map((g) => [g.key, Object.fromEntries(g.items.map((i) => [i.id, g.suggestion.keep.includes(i.id) ? "keep" : "remove"]))]));
}

/** Pan/zoom shared by both panes so the same detail is compared side by side. */
function useSyncedZoom() {
  const [view, setView] = useState({ scale: 1, x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number } | null>(null);
  const reset = useCallback(() => setView({ scale: 1, x: 0, y: 0 }), []);
  const zoom = useCallback((factor: number) => setView((v) => {
    const scale = Math.min(8, Math.max(1, v.scale * factor));
    return scale === 1 ? { scale: 1, x: 0, y: 0 } : { ...v, scale };
  }), []);
  const handlers = {
    onWheel: (e: WheelEvent) => { e.preventDefault(); zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15); },
    onPointerDown: (e: PointerEvent) => { drag.current = { x: e.clientX, y: e.clientY }; (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); },
    onPointerMove: (e: PointerEvent) => {
      if (!drag.current) return;
      const dx = e.clientX - drag.current.x, dy = e.clientY - drag.current.y;
      drag.current = { x: e.clientX, y: e.clientY };
      setView((v) => (v.scale === 1 ? v : { ...v, x: v.x + dx / v.scale, y: v.y + dy / v.scale }));
    },
    onPointerUp: () => { drag.current = null; },
  };
  return { view, zoom, reset, handlers };
}

function Pane({ item, decision, view, handlers, label, score }: {
  item: ResolverItem; decision: "keep" | "remove"; view: { scale: number; x: number; y: number };
  handlers: ReturnType<typeof useSyncedZoom>["handlers"]; label: string; score?: number;
}) {
  const t = useT();
  return (
    <figure className={`compare-pane ${decision}`}>
      <div className="compare-stage" {...handlers}>
        <img src={item.kind === "photo" ? `/api/media/${item.id}/preview` : `/api/media/${item.id}/thumbnail`} alt={item.name} draggable={false}
          style={{ transform: `scale(${view.scale}) translate(${view.x}px, ${view.y}px)` }} />
        <span className={`compare-badge ${decision}`}>{decision === "keep" ? t("dupes.keep") : t("dupes.remove")}</span>
      </div>
      <figcaption>
        <strong>{label}</strong> {item.name}
        <span className="muted small-text">
          {item.width && item.height ? `${item.width}×${item.height} · ` : ""}{bytes(item.size)} · {dateLabel(item.captured_at)}
          {score !== undefined ? ` · ${t("dupes.score", { score: Math.round(score * 100) })}` : ""}
        </span>
        {item.path && <span className="muted small-text compare-path">{item.path}</span>}
      </figcaption>
    </figure>
  );
}

/** Keyboard-first resolver: compare, accept or adjust the keep-best suggestion, resolve with undo. */
export function DuplicateResolver({ groups, onClose, title }: { groups: ResolverGroup[]; onClose: () => void; title: string }) {
  const t = useT();
  const [index, setIndex] = useState(0);
  const [other, setOther] = useState(1);
  const [decisions, setDecisions] = useState(() => initial(groups));
  const [freeSpace, setFreeSpace] = useState(false);
  const zoom = useSyncedZoom();
  const action = useAction();
  const { pushUndo } = useApp();
  const group = groups[Math.min(index, groups.length - 1)];
  const decision = group ? decisions[group.key] : {};
  const kept = group?.items.find((i) => decision[i.id] === "keep") ?? group?.items[0];
  const candidates = group?.items.filter((i) => i.id !== kept?.id) ?? [];
  const right = candidates[Math.min(other - 1, candidates.length - 1)] ?? candidates[0];

  const totals = useMemo(() => {
    let bytesFreed = 0, removed = 0;
    for (const g of groups) for (const i of g.items) if (decisions[g.key][i.id] === "remove") { bytesFreed += i.size; removed += 1; }
    return { bytesFreed, removed };
  }, [groups, decisions]);

  const set = useCallback((id: number, value: "keep" | "remove") => {
    if (!group) return;
    setDecisions((all) => {
      const next = { ...all[group.key], [id]: value };
      if (!Object.values(next).includes("keep")) return all; // a group always keeps at least one
      return { ...all, [group.key]: next };
    });
  }, [group]);

  const resolve = useCallback(() => void action.run(async () => {
    const payload = groups.map((g) => ({
      keep: g.items.filter((i) => decisions[g.key][i.id] === "keep").map((i) => i.id),
      remove: g.items.filter((i) => decisions[g.key][i.id] === "remove").map((i) => i.id),
    })).filter((g) => g.remove.length);
    const r = await mutate<{ audit_id: number; removed: number; moved_bytes: number }>("/duplicates/resolve", { groups: payload, free_space: freeSpace });
    pushUndo(t("dupes.resolved", { count: r.removed }), undoAudit(r.audit_id));
    refreshData();
    onClose();
  }), [action, groups, decisions, freeSpace, pushUndo, t, onClose]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && e.target.closest("input,textarea,select")) return;
      if (e.metaKey || e.ctrlKey || e.altKey || !group) return;
      const key = e.key.toLowerCase();
      const map: Record<string, () => void> = {
        arrowright: () => { setIndex((i) => Math.min(groups.length - 1, i + 1)); setOther(1); zoom.reset(); },
        arrowleft: () => { setIndex((i) => Math.max(0, i - 1)); setOther(1); zoom.reset(); },
        enter: () => { if (index < groups.length - 1) { setIndex(index + 1); setOther(1); zoom.reset(); } },
        tab: () => setOther((o) => (o % Math.max(1, candidates.length)) + 1),
        k: () => right && set(right.id, "keep"),
        x: () => right && set(right.id, "remove"),
        s: () => right && (set(right.id, "keep"), kept && set(kept.id, "remove")),
        "+": () => zoom.zoom(1.5), "=": () => zoom.zoom(1.5), "-": () => zoom.zoom(1 / 1.5), "0": zoom.reset,
        f: () => setFreeSpace((v) => !v),
        r: resolve,
      };
      if (/^[1-9]$/.test(key) && Number(key) <= candidates.length) { e.preventDefault(); setOther(Number(key)); return; }
      if (map[key]) { e.preventDefault(); map[key](); }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [group, groups.length, index, candidates.length, right, kept, set, zoom, resolve]);

  if (!group || !kept) return null;
  const scores = group.suggestion.scores;
  return (
    <Dialog open onClose={onClose} title={title} className="resolver-dialog" busy={action.busy}>
      <div className="dialog-body resolver">
        <div className="resolver-bar" role="status">
          <span>{t("dupes.group", { n: index + 1, total: groups.length })} · {group.type === "burst" ? t("dupes.burst") : group.type === "exact" ? t("dupes.exact") : t("dupes.similar", { pct: Math.round(group.similarity ?? 0) })}</span>
          <span>{t("dupes.totals", { count: number(totals.removed), size: bytes(totals.bytesFreed) })}</span>
        </div>
        <div className="compare">
          <Pane item={kept} decision="keep" view={zoom.view} handlers={zoom.handlers} label={t("dupes.left")} score={scores[String(kept.id)]} />
          {right && <Pane item={right} decision={decision[right.id]} view={zoom.view} handlers={zoom.handlers}
            label={t("dupes.right", { n: candidates.indexOf(right) + 1, total: candidates.length })} score={scores[String(right.id)]} />}
        </div>
        <p className="small-text muted">{t("dupes.suggested", { reason: group.suggestion.reason })}</p>
        <ul className="resolver-items" aria-label={t("dupes.items")}>
          {group.items.map((item) => (
            <li key={item.id}>
              <label className="check-label">
                <input type="checkbox" checked={decision[item.id] === "keep"} onChange={(e) => set(item.id, e.target.checked ? "keep" : "remove")} />
                {t("dupes.keep")} · {item.name} <span className="muted small-text">{bytes(item.size)}</span>
              </label>
            </li>
          ))}
        </ul>
        <label className="check-label">
          <input type="checkbox" checked={freeSpace} onChange={(e) => setFreeSpace(e.target.checked)} />
          {t("dupes.freeSpace")}
        </label>
        <div className="kbd-hints" aria-label={t("dupes.keys")}>
          <span><kbd>←</kbd><kbd>→</kbd> {t("dupes.keyGroups")}</span><span><kbd>Tab</kbd>/<kbd>1</kbd>–<kbd>9</kbd> {t("dupes.keyCompare")}</span>
          <span><kbd>K</kbd> {t("dupes.keep")}</span><span><kbd>X</kbd> {t("dupes.remove")}</span><span><kbd>S</kbd> {t("dupes.keySwap")}</span>
          <span><kbd>+</kbd><kbd>−</kbd><kbd>0</kbd> {t("dupes.keyZoom")}</span><span><kbd>F</kbd> {t("dupes.keyFree")}</span><span><kbd>R</kbd> {t("dupes.keyResolve")}</span>
        </div>
        <ErrorNotice error={action.error} />
      </div>
      <div className="dialog-footer">
        <button className="button" onClick={onClose}>{t("common.close")}</button>
        <button className="button primary" disabled={!totals.removed || action.busy} onClick={resolve}>
          {t("dupes.resolveAll", { count: number(totals.removed), size: bytes(totals.bytesFreed) })}
        </button>
      </div>
    </Dialog>
  );
}

/** Entry point on the Duplicates page: savings estimate + duplicates/bursts resolvers + bin. */
export function ResolverLauncher({ groups }: { groups: ResolverGroup[] }) {
  const t = useT();
  const bursts = useResource<{ groups: ResolverGroup[]; savings_bytes: number }>("/duplicates/bursts");
  const bin = useResource<{ bytes: number; files: number }>("/duplicates/bin");
  const [open, setOpen] = useState<"dupes" | "bursts" | null>(null);
  const [emptying, setEmptying] = useState(false);
  const savings = groups.reduce((n, g) => n + (g.suggestion?.savings_bytes ?? 0), 0);
  const resolvable = groups.filter((g) => g.suggestion);
  return (
    <section className="card-panel resolver-launch" aria-label={t("dupes.title")}>
      <div>
        <strong>{t("dupes.estimate", { size: bytes(savings), groups: number(resolvable.length) })}</strong>
        <p className="small-text muted">{t("dupes.estimateHelp")}</p>
      </div>
      <div className="inline-actions">
        <button className="button primary" disabled={!resolvable.length} onClick={() => setOpen("dupes")}>{t("dupes.review")}</button>
        <button className="button" disabled={!bursts.data?.groups.length} onClick={() => setOpen("bursts")}>
          {t("dupes.reviewBursts", { count: number(bursts.data?.groups.length ?? 0) })}
        </button>
        {!!bin.data?.files && (
          <button className="button danger-text" onClick={() => setEmptying(true)}>
            {t("dupes.emptyBin", { size: bytes(bin.data.bytes) })}
          </button>
        )}
      </div>
      <ConfirmDialog open={emptying} onClose={() => setEmptying(false)} title={t("dupes.emptyBin", { size: bytes(bin.data?.bytes ?? 0) })}
        description={t("dupes.emptyPrompt", { size: bytes(bin.data?.bytes ?? 0) })} label={t("dupes.emptyConfirm")} confirmation="EMPTY"
        onConfirm={() => mutate("/duplicates/bin/empty", { confirm: "EMPTY" })} success={t("dupes.emptied")} />
      {open === "dupes" && <DuplicateResolver groups={resolvable} title={t("dupes.title")} onClose={() => setOpen(null)} />}
      {open === "bursts" && bursts.data && <DuplicateResolver groups={bursts.data.groups} title={t("dupes.burstsTitle")} onClose={() => setOpen(null)} />}
    </section>
  );
}
