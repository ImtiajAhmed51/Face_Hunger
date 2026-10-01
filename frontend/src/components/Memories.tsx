import { useState } from "react";
import { Link } from "react-router-dom";
import { mutate, number } from "../api";
import { refreshData, useResource } from "../hooks";
import { useT } from "../i18n";
import { MediaViewer } from "./MediaViewer";
import { Thumbnail } from "./ui";

interface Section { kind: "on_this_day" | "event"; title: string; year: number; total: number; media_ids: number[]; event_id?: number }

/** "On this day" and this-week events from earlier years, best shots first. */
export function Memories() {
  const t = useT();
  const data = useResource<{ sections: Section[] }>("/memories");
  const [open, setOpen] = useState<{ id: number; ids: number[] } | null>(null);
  const sections = data.data?.sections ?? [];
  if (!sections.length) return null;
  return (
    <section className="home-section memories" aria-labelledby="memories-heading">
      <div className="section-heading"><div><p className="eyebrow">{t("memories.eyebrow")}</p><h2 id="memories-heading">{t("memories.title")}</h2></div></div>
      {sections.map((s) => (
        <div key={`${s.kind}-${s.event_id ?? s.year}`} className="memory">
          <h3>
            {s.event_id ? <Link to={`/events/${s.event_id}`}>{s.title}</Link>
              : (new Date().getFullYear() - s.year === 1 ? t("memories.yearAgo")
                : t("memories.yearsAgo", { count: number(new Date().getFullYear() - s.year) }))}
            <span className="muted small-text"> · {s.year}</span>
          </h3>
          <div className="memory-strip">
            {s.media_ids.map((id) => (
              <button key={id} onClick={() => setOpen({ id, ids: s.media_ids })}
                aria-label={t("memories.open", { year: s.year })}>
                <Thumbnail src={`/api/media/${id}/thumbnail`} alt="" />
              </button>
            ))}
            {s.total > s.media_ids.length && <span className="memory-more muted small-text">{t("memories.more", { count: number(s.total - s.media_ids.length) })}</span>}
          </div>
        </div>
      ))}
      {open && <MediaViewer id={open.id} ids={open.ids} onClose={() => setOpen(null)} />}
    </section>
  );
}

interface AuditEntry { id: number; at: string; action: string; summary: string; item_count: number; undone_at: string | null; undoable: boolean }

export function ActivityLog() {
  const t = useT();
  const log = useResource<{ items: AuditEntry[] }>("/audit?limit=50");
  const [busy, setBusy] = useState<number | null>(null);
  const items = log.data?.items ?? [];
  return (
    <section className="settings-section" aria-labelledby="activity-heading">
      <div className="section-heading"><div><h2 id="activity-heading">{t("activity.title")}</h2><p>{t("activity.help")}</p></div></div>
      {!items.length ? <p className="muted small-text">{t("activity.empty")}</p> : (
        <ul className="health-backups activity-log">
          {items.map((e) => (
            <li key={e.id}>
              <span>{e.summary}<span className="muted small-text"> · {new Date(e.at.replace(" ", "T") + "Z").toLocaleString()}</span></span>
              {e.action !== "undo" && (e.undone_at ? <span className="muted small-text">{t("activity.undone")}</span> : (
                <button className="text-link" disabled={busy === e.id} onClick={async () => {
                  setBusy(e.id);
                  try {
                    await mutate(`/audit/${e.id}/undo`);
                  } finally {
                    setBusy(null);
                    refreshData();
                  }
                }}>{t("activity.undo")}</button>
              ))}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
