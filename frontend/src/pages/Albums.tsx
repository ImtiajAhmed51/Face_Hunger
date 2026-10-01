import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { mutate, number, request } from "../api";
import { undoAudit, type Audited } from "../audit";
import { useAction, useApp } from "../context";
import { useResource, useSelection } from "../hooks";
import { useT } from "../i18n";
import type { HybridMedia, HybridResult, Media } from "../types";
import { Icon } from "../components/Icon";
import { MediaCollection, VirtualMediaGrid } from "../components/MediaGrid";
import { MediaViewer } from "../components/MediaViewer";
import { ConfirmDialog, Empty, ErrorNotice, GallerySkeleton, PageHeader, Thumbnail } from "../components/ui";
import { useWindowedPages } from "../virtual/useWindowedPages";
import { idsBetween } from "../virtual/geometry";

interface Album { id: number; name: string; cover_media_id: number | null; item_count: number }
interface Collection { id: number; name: string; item_count: number; preview_ids: number[] }

function Tile({ to, cover, title, meta }: { to: string; cover: number | null; title: string; meta: string }) {
  return (
    <Link to={to} className="album-tile">
      <span className="album-cover"><Thumbnail src={cover ? `/api/media/${cover}/thumbnail` : null} alt="" /></span>
      <strong>{title}</strong>
      <span className="muted small-text">{meta}</span>
    </Link>
  );
}

export function Albums() {
  const t = useT();
  const albums = useResource<{ items: Album[] }>("/albums");
  const collections = useResource<{ items: Collection[] }>("/collections");
  const favorites = useResource<{ total: number; items: Media[] }>("/media?favorite=true&limit=1");
  const action = useAction();
  const { pushUndo } = useApp();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  return (
    <>
      <PageHeader eyebrow={t("library.albumsEyebrow")} title={t("library.albums")} description={t("library.albumsDescription")} />
      <form className="inline-actions album-create" onSubmit={(e) => {
        e.preventDefault();
        if (!name.trim()) return;
        void action.run(async () => {
          const r = await mutate<Audited & { album_id: number }>("/albums", { name: name.trim() });
          pushUndo(t("library.created", { name: name.trim() }), undoAudit(r.audit_id));
          setName("");
          navigate(`/albums/${r.album_id}`);
        });
      }}>
        <label className="field">{t("library.newAlbum")}
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("library.albumName")} />
        </label>
        <button className="button primary" disabled={!name.trim() || action.busy}><Icon name="plus" size={16} />{t("library.create")}</button>
      </form>
      <ErrorNotice error={albums.error || collections.error} retry={() => { albums.reload(); collections.reload(); }} />
      {albums.loading ? <GallerySkeleton /> : (
        <div className="album-grid">
          <Tile to="/favorites" cover={favorites.data?.items[0]?.id ?? null} title={t("library.favorites")}
            meta={t("library.items", { count: number(favorites.data?.total ?? 0) })} />
          {(albums.data?.items ?? []).map((a) => (
            <Tile key={a.id} to={`/albums/${a.id}`} cover={a.cover_media_id} title={a.name} meta={t("library.items", { count: number(a.item_count) })} />
          ))}
        </div>
      )}
      {!albums.loading && !albums.data?.items.length && (
        <Empty icon="photo" title={t("library.emptyAlbums")} description={t("library.emptyAlbumsDescription")} />
      )}
      <section aria-labelledby="smart-heading">
        <div className="section-heading"><div><h2 id="smart-heading">{t("library.smart")}</h2><p>{t("library.smartHelp")}</p></div></div>
        <div className="album-grid">
          {(collections.data?.items ?? []).map((c) => (
            <Tile key={c.id} to={`/collections/${c.id}`} cover={c.preview_ids[0] ?? null} title={c.name}
              meta={t("library.items", { count: number(c.item_count) })} />
          ))}
        </div>
      </section>
    </>
  );
}

export function AlbumDetail() {
  const t = useT();
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const album = useResource<Album>(`/albums/${id}`);
  const windowed = useWindowedPages<Media>(`/media?album=${id}`, 120);
  const selection = useSelection(`album-${id}`);
  const [anchor, setAnchor] = useState<number | null>(null);
  const [viewer, setViewer] = useState<number | null>(null);
  const [rename, setRename] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const action = useAction();
  const { pushUndo } = useApp();
  const ids = Array.from(selection.selected);
  if (album.error) return <Empty icon="alert" title={t("library.emptyAlbum")} description={album.error}><Link className="button" to="/albums">{t("library.back")}</Link></Empty>;
  return (
    <>
      <p><Link className="text-link" to="/albums"><Icon name="back" size={14} />{t("library.back")}</Link></p>
      {album.data && (rename === null ? (
        <PageHeader title={album.data.name} description={t("library.items", { count: number(album.data.item_count) })}
          actions={<div className="inline-actions">
            <button className="button small" onClick={() => setRename(album.data!.name)}><Icon name="edit" size={16} />{t("library.rename")}</button>
            <button className="button small danger-text" onClick={() => setConfirm(true)}><Icon name="trash" size={16} />{t("library.delete")}</button>
          </div>} />
      ) : (
        <form className="page-header" onSubmit={(e) => {
          e.preventDefault();
          void action.run(async () => {
            const r = await mutate<Audited>(`/albums/${id}`, { name: rename }, "PATCH");
            pushUndo(t("library.renamed"), undoAudit(r.audit_id));
            setRename(null);
          });
        }}>
          <label className="field wide-field">{t("library.rename")}
            <input autoFocus value={rename} onChange={(e) => setRename(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") setRename(null); }} />
          </label>
          <button className="button primary" disabled={!rename.trim()}>{t("events.save")}</button>
        </form>
      ))}
      {ids.length > 0 && (
        <div className="selection-toolbar" role="region" aria-label="Selected media actions">
          <strong>{number(ids.length)} selected</strong>
          <button className="button small" disabled={action.busy} onClick={() => void action.run(async () => {
            const r = await mutate<Audited & { removed: number }>(`/albums/${id}/items/remove`, { media_ids: ids });
            selection.clear();
            pushUndo(t("library.removed", { count: r.removed }), undoAudit(r.audit_id));
          })}>{t("library.remove")}</button>
          <button className="button ghost small" onClick={selection.clear}>Clear selection</button>
        </div>
      )}
      <ErrorNotice error={windowed.error || action.error} retry={windowed.reload} />
      {windowed.loading ? <GallerySkeleton /> : !windowed.count ? (
        <Empty icon="photo" title={t("library.emptyAlbum")} description={t("library.emptyAlbumDescription")} />
      ) : (
        <VirtualMediaGrid windowed={windowed} selected={selection.selected} label={album.data?.name ?? t("library.albums")}
          onOpen={setViewer} onClear={selection.clear} onSelectAll={() => selection.addRange(windowed.loaded.map((m) => m.id))}
          onSelect={(mid, index, mode) => {
            if (mode === "range" && anchor !== null) selection.addRange(idsBetween(anchor, index, windowed.getItem));
            else { setAnchor(index); selection.toggle(mid); }
          }} />
      )}
      {viewer !== null && <MediaViewer id={viewer} ids={windowed.loaded.map((m) => m.id)} onClose={() => setViewer(null)} />}
      <ConfirmDialog open={confirm} onClose={() => setConfirm(false)} title={t("library.delete")} description={t("library.deleteHelp")}
        label={t("library.delete")} onConfirm={async () => {
          const r = await mutate<Audited>(`/albums/${id}`, undefined, "DELETE");
          pushUndo(t("library.deleted"), undoAudit(r.audit_id));
          navigate("/albums");
        }} />
    </>
  );
}

export function CollectionDetail() {
  const t = useT();
  const id = Number(useParams().id);
  const collections = useResource<{ items: Collection[] }>("/collections");
  const meta = collections.data?.items.find((c) => c.id === id);
  const windowed = useWindowedPages<HybridMedia>(`collection:${id}`, 120, (page, limit, signal) =>
    request<HybridResult>(`/collections/${id}/items?page=${page}&limit=${limit}`, { signal }));
  const selection = useSelection(`collection-${id}`);
  const [viewer, setViewer] = useState<number | null>(null);
  return (
    <>
      <p><Link className="text-link" to="/albums"><Icon name="back" size={14} />{t("library.back")}</Link></p>
      <PageHeader eyebrow={t("library.smart")} title={meta?.name ?? t("library.smart")} description={t("library.items", { count: number(windowed.count) })} />
      <ErrorNotice error={windowed.error} retry={windowed.reload} />
      {windowed.loading ? <GallerySkeleton /> : (
        <VirtualMediaGrid windowed={windowed as unknown as ReturnType<typeof useWindowedPages<Media>>} selected={selection.selected}
          label={meta?.name ?? t("library.smart")} onOpen={setViewer} onClear={selection.clear}
          onSelectAll={() => selection.addRange(windowed.loaded.map((m) => m.id))} onSelect={(mid) => selection.toggle(mid)} />
      )}
      {viewer !== null && <MediaViewer id={viewer} ids={windowed.loaded.map((m) => m.id)} onClose={() => setViewer(null)} />}
    </>
  );
}

export function Favorites() {
  const t = useT();
  return (
    <>
      <PageHeader eyebrow={t("library.albums").toUpperCase()} title={t("library.favorites")} description={t("library.favoritesDescription")} />
      <MediaCollection key="favorites" filters={{ favorite: true }} emptyTitle={t("library.favorites")}
        emptyDescription={t("library.favoritesDescription")} />
    </>
  );
}
