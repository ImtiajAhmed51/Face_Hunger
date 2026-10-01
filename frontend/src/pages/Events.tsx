import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { dateLabel, mutate, number } from "../api";
import { useAction, useApp } from "../context";
import { refreshData, useResource, useSelection } from "../hooks";
import { useT } from "../i18n";
import type { Media, Page } from "../types";
import { Icon } from "../components/Icon";
import { VirtualMediaGrid } from "../components/MediaGrid";
import { MediaViewer } from "../components/MediaViewer";
import { Badge, Dialog, Empty, ErrorNotice, GallerySkeleton, PageHeader, Thumbnail } from "../components/ui";
import { VirtualGrid } from "../virtual/VirtualGrid";
import { useWindowedPages } from "../virtual/useWindowedPages";
import { idsBetween } from "../virtual/geometry";

export interface EventItem {
  id: number;
  name: string;
  start_at: string;
  end_at: string;
  cover_media_id: number | null;
  item_count: number;
  user_edited: boolean;
  people: { id: number; display_name: string }[];
  undo_token?: number;
}

function range(event: EventItem): string {
  const a = dateLabel(event.start_at), b = dateLabel(event.end_at);
  return a === b ? a : `${a} – ${b}`;
}

function EventCard({ event, selected, onToggle }: { event: EventItem; selected: boolean; onToggle: () => void }) {
  const t = useT();
  return (
    <article className={`event-card ${selected ? "selected" : ""}`}>
      <Link to={`/events/${event.id}`} className="event-cover" tabIndex={-1} aria-label={`${event.name}, ${range(event)}`}>
        <Thumbnail src={event.cover_media_id ? `/api/media/${event.cover_media_id}/thumbnail` : null} alt="" />
      </Link>
      <label className="select-check">
        <input type="checkbox" checked={selected} onChange={onToggle} aria-label={t("events.select", { name: event.name })} />
      </label>
      <div className="event-caption">
        <strong title={event.name}>{event.name}</strong>
        <span className="muted small-text">{range(event)} · {t("events.items", { count: number(event.item_count) })}</span>
        <span className="event-people">
          {event.people.slice(0, 3).map((p) => <Badge key={p.id}>{p.display_name}</Badge>)}
          {event.user_edited && <Badge tone="teal">{t("events.edited")}</Badge>}
        </span>
      </div>
    </article>
  );
}

function useEventUndo() {
  const { pushUndo } = useApp();
  return (label: string, token?: number) => {
    if (token !== undefined) pushUndo(label, () => mutate(`/events/undo/${token}`));
  };
}

export function Events() {
  const t = useT();
  const navigate = useNavigate();
  const windowed = useWindowedPages<EventItem>("/events", 60);
  const selection = useSelection("events");
  const action = useAction();
  const undoable = useEventUndo();
  const ids = Array.from(selection.selected);
  return (
    <>
      <PageHeader eyebrow={t("events.eyebrow")} title={t("events.title")} description={t("events.description")}
        actions={<button className="button" disabled={action.busy} onClick={() => void action.run(() => mutate("/events/detect", {}), t("events.detect"))}>
          <Icon name="spark" size={16} />{t("events.detect")}
        </button>} />
      <div className="collection-bar">
        <span className="muted small-text" role="status">{t("events.count", { count: number(windowed.count) })}</span>
        {ids.length >= 2 && (
          <button className="button small" disabled={action.busy} onClick={() => void action.run(async () => {
            const merged = await mutate<EventItem>("/events/merge", { event_ids: ids });
            selection.clear();
            undoable(t("events.merged"), merged.undo_token);
          })}>
            <Icon name="merge" size={16} />{t("events.merge", { count: ids.length })}
          </button>
        )}
      </div>
      <ErrorNotice error={windowed.error} retry={windowed.reload} />
      {windowed.loading ? <GallerySkeleton label={t("common.loading")} /> : !windowed.count ? (
        <Empty icon="photo" title={t("events.emptyTitle")} description={t("events.emptyDescription")} />
      ) : (
        <VirtualGrid<EventItem>
          label={t("events.title")} count={windowed.count} getItem={windowed.getItem} onRange={windowed.onRange}
          minCell={230} gap={16} aspect={0.75} extra={88}
          onOpen={(_, event) => navigate(`/events/${event.id}`)}
          onToggle={(_, event) => selection.toggle(event.id)} onEscape={selection.clear}
          renderCell={(event) => event
            ? <EventCard event={event} selected={selection.selected.has(event.id)} onToggle={() => selection.toggle(event.id)} />
            : <div className="cell-skeleton" aria-hidden="true" />}
        />
      )}
    </>
  );
}

export function EventDetail() {
  const t = useT();
  const { id } = useParams();
  const eventId = Number(id);
  const event = useResource<EventItem>(`/events/${eventId}`);
  const windowed = useWindowedPages<Media>(`/media?event=${eventId}&sort=date_asc`, 120);
  const selection = useSelection(`event-${eventId}`);
  const [anchor, setAnchor] = useState<number | null>(null);
  const [viewer, setViewer] = useState<number | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const action = useAction();
  const undoable = useEventUndo();
  const ids = Array.from(selection.selected);
  const firstSelected = windowed.loaded.find((m) => selection.selected.has(m.id));

  if (event.error) return <Empty icon="alert" title={t("events.notFound")} description={event.error}><Link className="button" to="/events">{t("events.back")}</Link></Empty>;
  const data = event.data;
  return (
    <>
      <p><Link className="text-link" to="/events"><Icon name="back" size={14} />{t("events.back")}</Link></p>
      {data && (name === null ? (
        <PageHeader eyebrow={range(data)} title={data.name}
          description={t("events.items", { count: number(data.item_count) })}
          actions={<button className="button small" onClick={() => setName(data.name)}><Icon name="edit" size={16} />{t("events.rename")}</button>} />
      ) : (
        <form className="page-header" onSubmit={(e) => {
          e.preventDefault();
          void action.run(async () => {
            const renamed = await mutate<EventItem>(`/events/${eventId}`, { name }, "PATCH");
            setName(null);
            undoable(t("events.renamed"), renamed.undo_token);
          });
        }}>
          <label className="field wide-field">{t("events.rename")}
            <input autoFocus value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") setName(null); }} />
          </label>
          <button className="button primary" disabled={!name.trim() || action.busy}>{t("events.save")}</button>
        </form>
      ))}
      {data && data.people.length > 0 && <p className="event-people">{data.people.map((p) => <Link key={p.id} to={`/people/${p.id}`}><Badge>{p.display_name}</Badge></Link>)}</p>}
      <div className="collection-bar">
        <span className="muted small-text">{ids.length ? t("common.selected", { count: number(ids.length) }) : t("events.splitHelp")}</span>
        <div className="inline-actions">
          <button className="button small" disabled={!firstSelected || action.busy} onClick={() => firstSelected && void action.run(async () => {
            const result = await mutate<{ undo_token: number }>(`/events/${eventId}/split`, { media_id: firstSelected.id });
            selection.clear();
            undoable(t("events.splitDone"), result.undo_token);
          })}>{t("events.split")}</button>
          <button className="button small" disabled={!ids.length || action.busy} onClick={() => setPicking(true)}>{t("events.move")}</button>
        </div>
      </div>
      <ErrorNotice error={windowed.error || action.error} retry={windowed.reload} />
      {windowed.loading ? <GallerySkeleton /> : (
        <VirtualMediaGrid windowed={windowed} selected={selection.selected} label={data?.name ?? t("events.title")}
          onOpen={setViewer} onClear={selection.clear} onSelectAll={() => selection.addRange(windowed.loaded.map((m) => m.id))}
          onSelect={(mid, index, mode) => {
            if (mode === "range" && anchor !== null) selection.addRange(idsBetween(anchor, index, windowed.getItem));
            else { setAnchor(index); selection.toggle(mid); }
          }} />
      )}
      {picking && <MoveDialog exclude={eventId} onClose={() => setPicking(false)} onPick={(target) => void action.run(async () => {
        const moved = await mutate<EventItem>(`/events/${target}/move`, { media_ids: ids });
        setPicking(false);
        selection.clear();
        undoable(t("events.moved", { count: ids.length }), moved.undo_token);
        refreshData();
      })} />}
      {viewer !== null && <MediaViewer id={viewer} ids={windowed.loaded.map((m) => m.id)} onClose={() => setViewer(null)} />}
    </>
  );
}

function MoveDialog({ exclude, onClose, onPick }: { exclude: number; onClose: () => void; onPick: (id: number) => void }) {
  const t = useT();
  const list = useResource<Page<EventItem>>("/events?limit=200");
  return (
    <Dialog open onClose={onClose} title={t("events.pickTarget")}>
      <ul className="dialog-body event-pick">
        {(list.data?.items ?? []).filter((e) => e.id !== exclude).map((e) => (
          <li key={e.id}><button className="button ghost" onClick={() => onPick(e.id)}>{e.name}<span className="muted small-text"> · {range(e)}</span></button></li>
        ))}
      </ul>
    </Dialog>
  );
}
