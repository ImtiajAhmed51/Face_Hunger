import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { mutate, number } from "../api";
import { useAction } from "../context";
import { useResource } from "../hooks";
import type { Media } from "../types";
import { Icon } from "../components/Icon";
import { MediaViewer } from "../components/MediaViewer";
import {
  Badge,
  ConfirmDialog,
  Dialog,
  Empty,
  ErrorNotice,
  Loading,
  PageHeader,
  Thumbnail,
} from "../components/ui";

interface DupItem extends Media {
  similarity?: number;
  path?: string;
}

interface DupGroup {
  type: "exact" | "near";
  key: string;
  similarity?: number;
  items: DupItem[];
}

interface DuplicatesResponse {
  groups: DupGroup[];
  threshold: number;
  dino: {
    available: boolean;
    device: string | null;
    dim: number;
    error: string | null;
  };
  embedding_total: number;
  embedding_filled: number;
  embedding_remaining: number;
  group_count: number;
  item_count: number;
}

function formatSize(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function shortPath(path?: string) {
  if (!path) return "";
  const parts = path.replace(/\\/g, "/").split("/");
  if (parts.length <= 3) return path;
  return `…/${parts.slice(-2).join("/")}`;
}

const GroupCard = memo(function GroupCard({
  group,
  selected,
  onToggle,
  onSelectAll,
  onOpen,
  onKeep,
  onDelete,
  onReveal,
  onMove,
}: {
  group: DupGroup;
  selected: Set<number>;
  onToggle: (id: number) => void;
  onSelectAll: (ids: number[]) => void;
  onOpen: (item: DupItem) => void;
  onKeep: (keepId: number, deleteIds: number[]) => void;
  onDelete: (ids: number[]) => void;
  onReveal: (id: number) => void;
  onMove: (ids: number[]) => void;
}) {
  const ids = group.items.map((i) => i.id);
  const allSelected = ids.every((id) => selected.has(id));
  const someSelected = ids.some((id) => selected.has(id));

  return (
    <article className="dino-group">
      <header className="dino-group-header">
        <div className="dino-group-title">
          <Badge tone={group.type === "exact" ? "red" : "amber"}>
            {group.type === "exact" ? "Exact copy" : "Near duplicate"}
          </Badge>
          {group.type === "near" && group.similarity != null && (
            <span className="dino-sim-pill">
              {group.similarity.toFixed(0)}% similar
            </span>
          )}
          <span className="dino-group-count">
            {group.items.length} file{group.items.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="dino-group-tools">
          <button
            type="button"
            className="button small ghost"
            onClick={() => onSelectAll(allSelected ? [] : ids)}
          >
            {allSelected ? "Deselect" : "Select all"}
          </button>
          {someSelected && (
            <>
              <button
                type="button"
                className="button small"
                onClick={() => onMove(ids.filter((id) => selected.has(id)))}
              >
                Move
              </button>
              <button
                type="button"
                className="button small danger"
                onClick={() => onDelete(ids.filter((id) => selected.has(id)))}
              >
                Soft-delete
              </button>
            </>
          )}
        </div>
      </header>

      <div className="dino-items">
        {group.items.map((item) => {
          const isSelected = selected.has(item.id);
          return (
            <div
              key={item.id}
              className={`dino-item ${isSelected ? "selected" : ""}`}
            >
              <div className="dino-thumb-wrap">
                <button
                  type="button"
                  className="dino-thumb"
                  onClick={() => onOpen(item)}
                  title="Preview"
                >
                  <Thumbnail
                    src={`/api/media/${item.id}/thumbnail`}
                    alt={item.name}
                    icon={item.kind === "video" ? "video" : "photo"}
                  />
                  {item.kind === "video" && (
                    <span className="play-badge" aria-hidden>
                      <Icon name="play" size={14} />
                    </span>
                  )}
                  {item.similarity != null && group.type === "near" && (
                    <span className="dino-thumb-sim">
                      {item.similarity.toFixed(0)}%
                    </span>
                  )}
                </button>
                <label className="dino-check">
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => onToggle(item.id)}
                    aria-label={`Select ${item.name}`}
                  />
                </label>
              </div>

              <div className="dino-meta">
                <p className="dino-name" title={item.name}>
                  {item.name}
                </p>
                <p className="dino-specs">
                  <span className="dino-kind">{item.kind}</span>
                  <span>{formatSize(item.size)}</span>
                  {item.width && item.height ? (
                    <span>
                      {item.width}×{item.height}
                    </span>
                  ) : null}
                </p>
                {item.path && (
                  <p className="dino-path" title={item.path}>
                    {shortPath(item.path)}
                  </p>
                )}
                <div className="dino-actions">
                  <button
                    type="button"
                    className="button tiny ghost"
                    onClick={() => onOpen(item)}
                  >
                    Preview
                  </button>
                  <button
                    type="button"
                    className="button tiny primary"
                    onClick={() =>
                      onKeep(
                        item.id,
                        ids.filter((id) => id !== item.id),
                      )
                    }
                    title="Keep this, soft-delete the others"
                  >
                    Keep
                  </button>
                  <button
                    type="button"
                    className="button tiny ghost"
                    onClick={() => onReveal(item.id)}
                  >
                    Reveal
                  </button>
                  <button
                    type="button"
                    className="button tiny ghost"
                    onClick={() => onMove([item.id])}
                  >
                    Move
                  </button>
                  <button
                    type="button"
                    className="button tiny danger"
                    onClick={() => onDelete([item.id])}
                  >
                    Delete
                  </button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </article>
  );
});

export function Duplicates() {
  const [threshold, setThreshold] = useState<number | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [viewer, setViewer] = useState<DupItem | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<number[] | null>(null);
  const [moveIds, setMoveIds] = useState<number[] | null>(null);
  const [moveDest, setMoveDest] = useState("");
  const [backfilling, setBackfilling] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [visibleCount, setVisibleCount] = useState(12);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const { run } = useAction();

  const qs =
    threshold != null ? `?threshold=${threshold}&limit=2000` : "?limit=2000";
  const { data, error, loading, reload } = useResource<DuplicatesResponse>(
    `/duplicates${qs}`,
  );

  const groups = data?.groups ?? [];
  const visibleGroups = useMemo(
    () => groups.slice(0, visibleCount),
    [groups, visibleCount],
  );

  // Reset window when data/threshold changes
  useEffect(() => {
    setVisibleCount(12);
  }, [qs, groups.length]);

  // Infinite append while scrolling — keeps DOM light so mobile scroll stays fluid
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    if (visibleCount >= groups.length) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisibleCount((n) => Math.min(groups.length, n + 10));
        }
      },
      { root: null, rootMargin: "400px 0px", threshold: 0 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [visibleCount, groups.length]);
  const remaining = data?.embedding_remaining ?? 0;
  const filled = data?.embedding_filled ?? 0;
  const total = data?.embedding_total ?? 0;
  const progress = total > 0 ? Math.min(100, (filled / total) * 100) : 0;

  const toggle = useCallback((id: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const selectAll = useCallback((ids: number[]) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (ids.length === 0) return new Set();
      const allIn = ids.every((id) => next.has(id));
      if (allIn) ids.forEach((id) => next.delete(id));
      else ids.forEach((id) => next.add(id));
      return next;
    });
  }, []);

  const doDelete = async (ids: number[]) => {
    for (const id of ids) {
      await mutate(`/media/${id}`, undefined, "DELETE");
    }
    setSelected((prev) => {
      const next = new Set(prev);
      ids.forEach((id) => next.delete(id));
      return next;
    });
    setConfirmDelete(null);
    reload();
  };

  const doKeep = async (_keepId: number, deleteIds: number[]) => {
    if (!deleteIds.length) return;
    setConfirmDelete(deleteIds);
  };

  const doReveal = async (id: number) => {
    await run(async () => {
      await mutate(`/media/${id}/reveal`);
    }, "Revealed in file manager");
  };

  const doMove = async () => {
    if (!moveIds?.length || !moveDest.trim()) return;
    await run(async () => {
      await mutate("/media/move", {
        media_ids: moveIds,
        destination: moveDest.trim(),
      });
      setMoveIds(null);
      setMoveDest("");
      reload();
    }, `Moved ${moveIds.length} file(s)`);
  };

  const backfill = async () => {
    setBackfilling(true);
    setMsg(null);
    try {
      let totalFilled = 0;
      for (let i = 0; i < 50; i++) {
        const res = await mutate<{
          filled: number;
          remaining: number;
          failed: number;
          dino?: { available: boolean; error: string | null };
        }>("/duplicates/backfill", { limit: 20 });
        totalFilled += res.filled;
        setMsg(
          `Embedded ${totalFilled} media… ${res.remaining} remaining` +
            (res.dino && !res.dino.available
              ? ` (DINOv2: ${res.dino.error || "unavailable"})`
              : ""),
        );
        if (res.remaining === 0 || res.filled === 0) break;
        reload();
      }
      reload();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBackfilling(false);
    }
  };

  const effectiveThreshold = threshold ?? data?.threshold ?? 0.92;

  return (
    <>
      <PageHeader
        eyebrow="FIND COPIES FAST"
        title="Duplicates"
        description="Exact copies and visually similar photos/videos via DINOv2. Nothing is deleted automatically."
      />

      {error && <ErrorNotice error={error} retry={reload} />}

      <section className="dino-toolbar card-panel">
        <div className="dino-toolbar-row">
          <label className="dino-threshold">
            <span>Similarity</span>
            <input
              type="range"
              min={0.7}
              max={0.99}
              step={0.01}
              value={effectiveThreshold}
              onChange={(e) => setThreshold(parseFloat(e.target.value))}
            />
            <strong>{(effectiveThreshold * 100).toFixed(0)}%</strong>
          </label>
          <div className="dino-toolbar-actions">
            <button
              type="button"
              className="button small"
              onClick={() => reload()}
            >
              Refresh
            </button>
            <button
              type="button"
              className="button small primary"
              disabled={backfilling || remaining === 0}
              onClick={() => void backfill()}
            >
              {backfilling
                ? "Embedding…"
                : remaining > 0
                  ? `Embed remaining (${number(remaining)})`
                  : "Embeddings complete"}
            </button>
          </div>
        </div>

        {data && (
          <div className="dino-progress-block">
            <div className="dino-progress-track" aria-hidden>
              <div
                className="dino-progress-fill"
                style={{ width: `${progress}%` }}
              />
            </div>
            <div className="dino-progress-meta">
              <span>
                <strong>{number(filled)}</strong> / {number(total)} embedded
              </span>
              <span>
                <strong>{number(data.group_count)}</strong> groups ·{" "}
                {number(data.item_count)} files
              </span>
              <span className="dino-device">
                {data.dino?.available
                  ? `DINOv2 · ${data.dino.device || "cpu"}`
                  : data.dino?.error
                    ? `DINOv2 unavailable`
                    : "DINOv2"}
              </span>
            </div>
          </div>
        )}
        {msg && <p className="dino-status-msg">{msg}</p>}
      </section>

      {loading && !data && <Loading label="Scanning for duplicates" />}

      {!loading && groups.length === 0 && (
        <Empty
          icon="photo"
          title="No duplicates found"
          description={
            remaining > 0
              ? "Compute DINOv2 embeddings first, then refresh. Exact duplicates also appear once content hashes are filled."
              : "No exact or near-duplicate media at this threshold. Try lowering the similarity slider."
          }
        />
      )}

      <div className="dino-groups">
        {visibleGroups.map((g) => (
          <GroupCard
            key={g.key + g.type}
            group={g}
            selected={selected}
            onToggle={toggle}
            onSelectAll={selectAll}
            onOpen={setViewer}
            onKeep={doKeep}
            onDelete={(ids) => setConfirmDelete(ids)}
            onReveal={(id) => void doReveal(id)}
            onMove={setMoveIds}
          />
        ))}
        {visibleCount < groups.length && (
          <div ref={sentinelRef} className="dino-scroll-sentinel" aria-hidden>
            <span className="muted small-text">
              Showing {visibleCount} of {groups.length} groups…
            </span>
          </div>
        )}
      </div>

      {viewer && <MediaViewer id={viewer.id} onClose={() => setViewer(null)} />}

      <ConfirmDialog
        open={!!confirmDelete}
        onClose={() => setConfirmDelete(null)}
        title="Soft-delete media?"
        description={`Soft-delete ${confirmDelete?.length ?? 0} file(s) from the index. Original files on disk are never removed by this action.`}
        label="Soft-delete"
        onConfirm={async () => {
          if (confirmDelete) await doDelete(confirmDelete);
        }}
        success="Soft-deleted from index"
      />

      <Dialog
        open={!!moveIds}
        onClose={() => {
          setMoveIds(null);
          setMoveDest("");
        }}
        title="Move files"
      >
        <div className="dialog-body">
          <p className="muted small-text">
            Move {moveIds?.length ?? 0} file(s) to a folder under your home
            directory. Paths in the index will be updated.
          </p>
          <label className="field">
            <span>Destination folder</span>
            <input
              type="text"
              value={moveDest}
              onChange={(e) => setMoveDest(e.target.value)}
              placeholder="/Users/you/Pictures/Keep"
            />
          </label>
        </div>
        <div className="dialog-footer">
          <button
            type="button"
            className="button"
            onClick={() => {
              setMoveIds(null);
              setMoveDest("");
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            className="button primary"
            disabled={!moveDest.trim()}
            onClick={() => void doMove()}
          >
            Move
          </button>
        </div>
      </Dialog>
    </>
  );
}
