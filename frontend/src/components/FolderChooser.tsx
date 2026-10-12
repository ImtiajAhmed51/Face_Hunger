import { Button } from "@/components/primitives/button";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { bytes, mutate, number, request } from "../api";
import { useT, type MessageKey } from "../i18n";
import { Icon, type IconName } from "./Icon";
import { Dialog, ErrorNotice } from "./ui";

interface Place { name: string; path: string; group: "places" | "volumes" | "network" | "recent"; kind: string; is_network: boolean; server: string | null; share: string | null }
interface Roots { places: Place[]; native_picker: boolean; home: string; shares: { connect: boolean; discover: boolean; keychain: boolean; protocols: string[] } }
interface Entry { name: string; path: string; is_readable: boolean; is_network: boolean; has_children: boolean | null; counts?: { photos: number; videos: number } | "unknown" }
interface Listing { path: string; name: string; parent: string | null; breadcrumbs: { name: string; path: string }[]; kind: string; is_network: boolean; server: string | null; share: string | null; is_readable: boolean; items: Entry[]; truncated: boolean }
interface Summary { path: string; kind: string; is_network: boolean; is_readable: boolean; photos: number; videos: number; complete: boolean; free_bytes_local: number; already_library: { id: number; name: string } | null }
export interface ChosenFolder { path: string; name: string }

const GROUPS: Place["group"][] = ["places", "volumes", "network", "recent"];
const groupIcon: Record<Place["group"], IconName> = { places: "folder", volumes: "drive", network: "layers", recent: "restore" };
const message = (error: unknown) => error instanceof Error ? error.message : String(error);

/** Connect to a share on another computer. The password goes to the server for this one call only. */
export function ConnectShareDialog({ open, onClose, onConnected, capabilities }: {
  open: boolean; onClose: () => void; onConnected: (mountPoint: string) => void; capabilities?: Roots["shares"];
}) {
  const t = useT();
  const [protocol, setProtocol] = useState("smb");
  const [server, setServer] = useState("");
  const [share, setShare] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [guest, setGuest] = useState(false);
  const [remember, setRemember] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [found, setFound] = useState<{ name: string; server: string }[] | null>(null);
  const [searching, setSearching] = useState(false);
  useEffect(() => { if (open) { setError(""); setPassword(""); } }, [open]);
  const discover = () => {
    setSearching(true);
    request<{ items: { name: string; server: string }[] }>("/shares/discover")
      .then((r) => setFound(r.items)).catch((e) => setError(message(e))).finally(() => setSearching(false));
  };
  return (
    <Dialog open={open} onClose={onClose} title={t("netshare.connectTitle")} busy={busy} className="share-dialog">
      <form onSubmit={(event) => {
        event.preventDefault();
        setBusy(true);
        setError("");
        mutate<{ mount_point: string }>("/shares/connect", { protocol, server: server.trim(), share: share.trim(), guest, remember: remember && !guest,
          username: guest ? undefined : username.trim() || undefined, password: guest ? undefined : password || undefined })
          .then((mount) => { setPassword(""); onConnected(mount.mount_point); })
          .catch((e) => setError(message(e)))
          .finally(() => setBusy(false));
      }}>
        <div className="dialog-body share-form">
          <p className="muted">{t("netshare.connectHelp")}</p>
          <div className="share-row">
            <label className="field">{t("netshare.protocol")}
              <select value={protocol} onChange={(e) => setProtocol(e.target.value)}>
                {(capabilities?.protocols ?? ["smb"]).map((p) => <option key={p} value={p}>{t(`netshare.protocol.${p}` as MessageKey)}</option>)}
              </select>
            </label>
            <label className="field grow">{t("netshare.server")}
              <input value={server} onChange={(e) => setServer(e.target.value)} required autoFocus spellCheck={false} autoCapitalize="off" autoComplete="off" />
            </label>
          </div>
          <p className="small-text muted">{t("netshare.serverHint")}</p>
          {capabilities?.discover && (
            <div className="share-discover">
              <Button type="button" className="button small" onClick={discover} disabled={searching}>{searching ? t("netshare.searching") : t("netshare.discover")}</Button>
              {found && (found.length === 0 ? <span className="small-text muted" role="status">{t("netshare.noneFound")}</span>
                : <ul className="share-found" aria-label={t("netshare.found")}>{found.map((f) => (
                  <li key={f.server}><Button type="button" className="button small ghost" onClick={() => setServer(f.server)}>{f.name}</Button></li>))}</ul>)}
            </div>
          )}
          <label className="field">{t("netshare.shareName")}
            <input value={share} onChange={(e) => setShare(e.target.value)} required spellCheck={false} autoComplete="off" />
          </label>
          <label className="check-label"><input type="checkbox" checked={guest} onChange={(e) => setGuest(e.target.checked)} />{t("netshare.guest")}</label>
          {!guest && (
            <>
              <div className="share-row">
                <label className="field grow">{t("netshare.username")}
                  <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" spellCheck={false} autoCapitalize="off" />
                </label>
                <label className="field grow">{t("netshare.password")}
                  <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
                </label>
              </div>
              {capabilities?.keychain && <label className="check-label"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />{t("netshare.remember")}</label>}
            </>
          )}
          <p className="small-text muted">{t("netshare.readOnlyNote")}</p>
          <ErrorNotice error={error} />
        </div>
        <div className="dialog-footer">
          <Button type="button" className="button" onClick={onClose} disabled={busy}>{t("common.cancel")}</Button>
          <Button className="button primary" disabled={busy || !server.trim() || !share.trim()}>{busy ? t("netshare.connecting") : t("netshare.connect")}</Button>
        </div>
      </form>
    </Dialog>
  );
}

/**
 * Pick folders on the computer running Face Hunger: places on the left, breadcrumbs and a folder
 * list on the right. Arrow keys move, Enter opens, Backspace goes up, Space ticks, Escape closes.
 */
export function FolderChooser({ open, onClose, onChoose, multiple = false, title, startPath, confirmLabel }: {
  open: boolean; onClose: () => void; onChoose: (folders: ChosenFolder[]) => void | Promise<void>;
  multiple?: boolean; title?: string; startPath?: string; confirmLabel?: string;
}) {
  const t = useT();
  const [roots, setRoots] = useState<Roots | null>(null);
  const [listing, setListing] = useState<Listing | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [active, setActive] = useState(0);
  const [checked, setChecked] = useState<Map<string, string>>(new Map());
  const [summary, setSummary] = useState<Summary | null>(null);
  const [connect, setConnect] = useState(false);
  const [busy, setBusy] = useState(false);
  const list = useRef<HTMLDivElement>(null);
  const ticket = useRef(0);

  const loadRoots = useCallback(() => request<Roots>("/fs/roots").then((r) => { setRoots(r); return r; }), []);
  const go = useCallback((path: string) => {
    const mine = ++ticket.current;
    setLoading(true);
    setError("");
    setFilter("");
    setSummary(null);
    request<Listing>(`/fs/list?path=${encodeURIComponent(path)}&include_counts=true`)
      .then((r) => { if (mine === ticket.current) { setListing(r); setActive(0); } })
      .catch((e) => { if (mine === ticket.current) setError(message(e)); })
      .finally(() => { if (mine === ticket.current) setLoading(false); });
  }, []);
  useEffect(() => {
    if (!open) return;
    setChecked(new Map());
    setListing(null);
    loadRoots().then((r) => go(startPath ?? r.home)).catch((e) => setError(message(e)));
  }, [open, startPath, go, loadRoots]);
  useEffect(() => {
    if (!listing) return;
    let cancelled = false;
    request<Summary>(`/fs/summary?path=${encodeURIComponent(listing.path)}`).then((s) => { if (!cancelled) setSummary(s); }).catch(() => {});
    return () => { cancelled = true; };
  }, [listing]);

  const items = useMemo(() => (listing?.items ?? []).filter((i) => i.name.toLowerCase().includes(filter.trim().toLowerCase())), [listing, filter]);
  useEffect(() => { list.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" }); }, [active]);
  const toggle = (entry: Entry) => setChecked((prev) => {
    const next = new Map(multiple ? prev : []);
    if (prev.has(entry.path)) next.delete(entry.path); else next.set(entry.path, entry.name);
    return next;
  });
  const onKey = (event: KeyboardEvent) => {
    if (event.target instanceof HTMLInputElement && event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Enter") return;
    const current = items[active];
    if (event.key === "ArrowDown") { event.preventDefault(); setActive((i) => Math.min(items.length - 1, i + 1)); }
    else if (event.key === "ArrowUp") { event.preventDefault(); setActive((i) => Math.max(0, i - 1)); }
    else if (event.key === "Home") { event.preventDefault(); setActive(0); }
    else if (event.key === "End") { event.preventDefault(); setActive(Math.max(0, items.length - 1)); }
    else if (event.key === "Enter" && current) { event.preventDefault(); if (current.is_readable) go(current.path); }
    else if (event.key === "Backspace" && listing?.parent) { event.preventDefault(); go(listing.parent); }
    else if (event.key === " " && current) { event.preventDefault(); toggle(current); }
  };
  const chosen: ChosenFolder[] = checked.size ? [...checked].map(([path, name]) => ({ path, name })) : listing ? [{ path: listing.path, name: listing.name }] : [];
  const confirm = () => {
    setBusy(true);
    setError("");
    mutate("/fs/allow", { paths: chosen.map((c) => c.path) })
      .then(() => onChoose(chosen)).then(onClose)
      .catch((e) => setError(message(e))).finally(() => setBusy(false));
  };
  const nativePick = () => {
    setBusy(true);
    mutate<{ cancelled: boolean; path?: string }>("/fs/native-pick")
      .then((r) => { if (!r.cancelled && r.path) go(r.path); })
      .catch((e) => setError(message(e))).finally(() => setBusy(false));
  };

  return (
    <Dialog open={open} onClose={onClose} title={title ?? t("chooser.title")} busy={busy} className="folder-chooser">
      <div className="chooser" onKeyDown={onKey}>
        <nav className="chooser-places" aria-label={t("chooser.places")}>
          {GROUPS.map((group) => {
            const places = (roots?.places ?? []).filter((p) => p.group === group);
            if (!places.length && group !== "network") return null;
            return (
              <div key={group} className="chooser-group">
                <h3>{t(`chooser.group.${group}` as MessageKey)}</h3>
                <ul>
                  {places.map((p) => (
                    <li key={`${group}:${p.path}`}>
                      <Button type="button" className={`chooser-place ${listing?.path === p.path ? "active" : ""}`} onClick={() => go(p.path)} title={p.path}>
                        <Icon name={groupIcon[group]} size={16} /><span>{p.name}</span>
                      </Button>
                    </li>
                  ))}
                  {group === "network" && !places.length && <li className="small-text muted chooser-none">{t("chooser.noShares")}</li>}
                </ul>
                {group === "network" && roots?.shares.connect && (
                  <Button type="button" className="button small chooser-connect" onClick={() => setConnect(true)}><Icon name="plus" size={14} />{t("chooser.connectShare")}</Button>
                )}
              </div>
            );
          })}
        </nav>
        <section className="chooser-main" aria-label={t("chooser.folders")}>
          <nav className="chooser-crumbs" aria-label={t("chooser.breadcrumbs")}>
            {listing?.parent && <Button type="button" className="icon-button" onClick={() => go(listing.parent!)} aria-label={t("chooser.up")}><Icon name="back" size={16} /></Button>}
            <ol>
              {(listing?.breadcrumbs ?? []).slice(-5).map((c, i, all) => (
                <li key={c.path}>{i === all.length - 1 ? <strong aria-current="location">{c.name}</strong>
                  : <Button type="button" className="chooser-crumb" onClick={() => go(c.path)}>{c.name}</Button>}</li>
              ))}
            </ol>
            {listing?.is_network && <span className="badge">{t("chooser.network")}</span>}
          </nav>
          <div className="chooser-tools">
            <label className="sr-only" htmlFor="chooser-filter">{t("chooser.filter")}</label>
            <input id="chooser-filter" type="search" value={filter} onChange={(e) => { setFilter(e.target.value); setActive(0); }} placeholder={t("chooser.filter")} />
            {roots?.native_picker && <Button type="button" className="button small" onClick={nativePick} disabled={busy}>{t("chooser.finder")}</Button>}
          </div>
          {error && (
            <div className="chooser-error">
              <ErrorNotice error={error} retry={() => listing ? go(listing.path) : void loadRoots().then((r) => go(r.home)).catch(() => {})} />
            </div>
          )}
          {/* Always mounted, so keyboard focus survives moving between folders. Items hold real controls, so this is a group, not a listbox. */}
          <div className="chooser-list" role="group" aria-label={t("chooser.folders")} tabIndex={0} ref={list} aria-busy={loading}>
            {loading ? (
              <ul aria-hidden="true">{Array.from({ length: 7 }, (_, i) => <li key={i} className="chooser-skeleton" />)}</ul>
            ) : error ? null : items.length === 0 ? (
              <p className="chooser-empty" role="status">{filter ? t("chooser.noMatch") : t("chooser.noFolders")}</p>
            ) : (
              <ul>
                {items.map((entry, index) => (
                  <li key={entry.path} data-index={index} aria-current={index === active ? "true" : undefined}
                    className={`chooser-item ${index === active ? "active" : ""} ${entry.is_readable ? "" : "unreadable"}`}
                    onClick={() => setActive(index)} onDoubleClick={() => entry.is_readable && go(entry.path)}>
                    <input type="checkbox" checked={checked.has(entry.path)} onChange={() => toggle(entry)} disabled={!entry.is_readable}
                      aria-label={t("chooser.select", { name: entry.name })} tabIndex={-1} />
                    <Icon name="folder" size={18} />
                    <Button type="button" className="chooser-name" onClick={() => entry.is_readable && go(entry.path)} disabled={!entry.is_readable} tabIndex={-1}>{entry.name}</Button>
                    <span className="chooser-counts small-text muted">
                      {!entry.is_readable ? t("chooser.noAccess") : entry.counts === "unknown" || !entry.counts ? t("chooser.countUnknown")
                        : entry.counts.photos + entry.counts.videos === 0 ? (entry.has_children ? t("chooser.subfolders") : t("chooser.emptyFolder"))
                          : t("chooser.counts", { photos: number(entry.counts.photos), videos: number(entry.counts.videos) })}
                    </span>
                    {entry.has_children !== false && entry.is_readable && <Icon name="arrow" size={14} className="chooser-chevron" />}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="chooser-summary" role="status">
            <code className="chooser-path">{chosen.length === 1 ? chosen[0].path : t("chooser.manySelected", { count: number(chosen.length) })}</code>
            {chosen.length === 1 && summary && summary.path === chosen[0].path && (
              <span className="small-text muted">
                {summary.is_readable ? t("chooser.readable") : t("chooser.notReadable")}{" · "}
                {t(summary.complete ? "chooser.estimate" : "chooser.estimateAtLeast", { photos: number(summary.photos), videos: number(summary.videos) })}{" · "}
                {t("chooser.freeLocal", { size: bytes(summary.free_bytes_local) })}
                {summary.already_library && ` · ${t("chooser.alreadyLibrary", { name: summary.already_library.name })}`}
              </span>
            )}
          </div>
        </section>
      </div>
      <div className="dialog-footer">
        <span className="small-text muted chooser-keys">{t("chooser.keys")}</span>
        <Button type="button" className="button" onClick={onClose} disabled={busy}>{t("common.cancel")}</Button>
        <Button type="button" className="button primary" onClick={confirm} disabled={busy || !chosen.length || loading}>
          {confirmLabel ?? (chosen.length > 1 ? t("chooser.confirmMany", { count: number(chosen.length) }) : t("chooser.confirm"))}
        </Button>
      </div>
      <ConnectShareDialog open={connect} onClose={() => setConnect(false)} capabilities={roots?.shares}
        onConnected={(mountPoint) => { setConnect(false); void loadRoots(); go(mountPoint); }} />
    </Dialog>
  );
}

/** A button that opens the chooser. Replaces every "type a folder path" field. */
export function ChooseFolderButton({ onChoose, label, multiple, className = "button", startPath, disabled }: {
  onChoose: (folders: ChosenFolder[]) => void | Promise<void>; label?: string; multiple?: boolean; className?: string; startPath?: string; disabled?: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button type="button" className={className} onClick={() => setOpen(true)} disabled={disabled}><Icon name="folder" size={16} />{label ?? t("chooser.button")}</Button>
      <FolderChooser open={open} onClose={() => setOpen(false)} onChoose={onChoose} multiple={multiple} startPath={startPath} />
    </>
  );
}
