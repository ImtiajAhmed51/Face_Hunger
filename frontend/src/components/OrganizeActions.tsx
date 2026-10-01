import { useState } from "react";
import { mutate } from "../api";
import { undoAudit, type Audited } from "../audit";
import { useAction, useApp } from "../context";
import { useResource } from "../hooks";
import { useT } from "../i18n";
import { Icon } from "./Icon";
import { Dialog, ErrorNotice } from "./ui";

interface Album { id: number; name: string; item_count: number }

/** Favorite + Add to album for a selection; both are audited and undoable. */
export function OrganizeActions({ ids, onDone }: { ids: number[]; onDone?: () => void }) {
  const t = useT();
  const action = useAction();
  const { pushUndo } = useApp();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className="button small" disabled={action.busy || !ids.length} onClick={() => void action.run(async () => {
        const r = await mutate<Audited & { changed: number }>("/favorites", { media_ids: ids, favorite: true });
        pushUndo(t("library.favorited", { count: r.changed }), undoAudit(r.audit_id));
        onDone?.();
      })}>
        <Icon name="spark" size={16} />{t("library.favorite")}
      </button>
      <button className="button small" disabled={action.busy || !ids.length} onClick={() => setOpen(true)}>
        <Icon name="plus" size={16} />{t("library.addToAlbum")}
      </button>
      {open && <AddToAlbumDialog ids={ids} onClose={() => setOpen(false)} onDone={() => { setOpen(false); onDone?.(); }} />}
    </>
  );
}

export function AddToAlbumDialog({ ids, onClose, onDone }: { ids: number[]; onClose: () => void; onDone: () => void }) {
  const t = useT();
  const albums = useResource<{ items: Album[] }>("/albums");
  const action = useAction();
  const { pushUndo } = useApp();
  const [name, setName] = useState("");
  const add = (albumId: number, albumName: string) => void action.run(async () => {
    const r = await mutate<Audited & { added: number }>(`/albums/${albumId}/items`, { media_ids: ids });
    pushUndo(t("library.added", { count: r.added, name: albumName }), undoAudit(r.audit_id));
    onDone();
  });
  return (
    <Dialog open onClose={onClose} title={t("library.addToAlbum")} busy={action.busy}>
      <div className="dialog-body">
        <form className="inline-actions" onSubmit={(e) => {
          e.preventDefault();
          if (!name.trim()) return;
          void action.run(async () => {
            const r = await mutate<Audited>("/albums", { name: name.trim(), media_ids: ids });
            pushUndo(t("library.created", { name: name.trim() }), undoAudit(r.audit_id));
            onDone();
          });
        }}>
          <label className="field wide-field">{t("library.newAlbum")}
            <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder={t("library.albumName")} />
          </label>
          <button className="button primary" disabled={!name.trim() || action.busy}>{t("library.create")}</button>
        </form>
        <ul className="event-pick" aria-label={t("library.albums")}>
          {(albums.data?.items ?? []).map((a) => (
            <li key={a.id}><button className="button ghost" onClick={() => add(a.id, a.name)}>{a.name}
              <span className="muted small-text"> · {a.item_count}</span></button></li>
          ))}
        </ul>
        <ErrorNotice error={action.error} />
      </div>
    </Dialog>
  );
}
