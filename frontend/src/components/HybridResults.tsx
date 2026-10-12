import { Button } from "@/components/primitives/button";
import { useState } from "react";
import { mutate, number, request } from "../api";
import { undoAudit, type Audited } from "../audit";
import { OrganizeActions } from "./OrganizeActions";
import { useAction, useApp } from "../context";
import { useSelection } from "../hooks";
import type { HybridMedia, HybridQuery, HybridResult, Media } from "../types";
import { idsBetween } from "../virtual/geometry";
import { useWindowedPages } from "../virtual/useWindowedPages";
import { Icon } from "./Icon";
import { VirtualMediaGrid } from "./MediaGrid";
import { MediaViewer } from "./MediaViewer";
import { Badge, ConfirmDialog, Empty, ErrorNotice, GallerySkeleton } from "./ui";

const SIGNAL_LABELS: Record<string, string> = {
  text: "Content",
  similar_media: "Looks similar",
  similar_face: "Same face",
  recency: "Recent",
};

/** Virtualized hybrid-search results with selection and undoable delete. */
export function HybridResults({ query }: { query: HybridQuery }) {
  const key = JSON.stringify(query);
  const windowed = useWindowedPages<HybridMedia>(`hybrid:${key}`, 120, (page, limit, signal) =>
    request<HybridResult>("/search/hybrid", { method: "POST", body: { ...query, page, limit }, signal }));
  const first = windowed.firstPage as HybridResult | undefined;
  const selection = useSelection(key);
  const action = useAction();
  const { pushUndo } = useApp();
  const [viewer, setViewer] = useState<{ id: number; index: number } | null>(null);
  const openViewer = (id: number) => setViewer({ id, index: windowed.indexOf(item => item.id === id) });
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [anchor, setAnchor] = useState<number | null>(null);
  const ids = Array.from(selection.selected);

  if (windowed.loading) return <GallerySkeleton label="Searching your library" />;
  return (
    <section className="collection" aria-label="Search results">
      <div className="collection-bar">
        <div className="inline-actions">
          <span className="muted small-text" role="status">
            {windowed.count === 1 ? '1 result' : `${number(windowed.count)} results`}{first ? ` in ${Math.round(first.took_ms)} ms` : ""}
            {ids.length ? ` · ${number(ids.length)} selected` : ""}
          </span>
          <span className="search-signals" aria-label="Signals used">
            {first?.signals.map((signal) => <Badge key={signal} tone="teal">{SIGNAL_LABELS[signal] ?? signal}</Badge>)}
          </span>
        </div>
        {ids.length > 0 && (
          <div className="inline-actions" role="region" aria-label="Selected results actions">

            <OrganizeActions ids={ids} onDone={selection.clear} />
            <Button className="button small danger-text" disabled={action.busy} onClick={() => setDeleteOpen(true)}>
              <Icon name="trash" size={16} />Delete
            </Button>
            <Button className="button ghost small" onClick={selection.clear}>Clear selection</Button>
          </div>
        )}
      </div>
      {first?.warnings.map((warning) => (
        <div key={warning} className="search-unmatched" role="status">
          <Icon name="alert" size={18} />
          <div><p>{warning}</p></div>
        </div>
      ))}
      <ErrorNotice error={windowed.error} retry={windowed.reload} />
      {!windowed.count ? (
        <Empty icon="search" title="No moments match just yet"
          description="Try different words, a wider date range, ANY instead of ALL, or fewer people." />
      ) : (
        <VirtualMediaGrid
          windowed={windowed as unknown as ReturnType<typeof useWindowedPages<Media>>}
          selected={selection.selected}
          label="Search results"
          onOpen={openViewer}
          onClear={selection.clear}
          onSelectAll={() => selection.addRange(windowed.loaded.map((item) => item.id))}
          onDelete={() => { if (ids.length) setDeleteOpen(true); }}
          onSelect={(id, index, mode) => {
            if (mode === "range" && anchor !== null) selection.addRange(idsBetween(anchor, index, windowed.getItem));
            else if (mode === "paint") selection.addRange([id]);
            else { setAnchor(index); selection.toggle(id); }
          }}
        />
      )}
      {viewer !== null && (
        <MediaViewer id={viewer.id} onClose={() => setViewer(null)} paging={{ index: viewer.index, total: windowed.count,
          onStep: async delta => {
            const item = await windowed.getItemAt(viewer.index + delta);
            if (item) setViewer({ id: item.id, index: viewer.index + delta });
          } }} />
      )}
      <ConfirmDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        title={`Move ${ids.length} item(s) to Deleted?`}
        description="Original files stay on disk. You can undo this, or restore them later from the Deleted tab."
        label="Delete"
        onConfirm={async () => {
          const target = [...ids];
          const result = await mutate<Audited>("/batch/delete", { media_ids: target });
          selection.clear();
          pushUndo(`Moved ${target.length} item(s) to Deleted`, undoAudit(result.audit_id));
        }}
      />
    </section>
  );
}
