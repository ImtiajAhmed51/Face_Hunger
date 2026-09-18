import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { mutate, number, request } from "../api";
import { useAction } from "../context";
import { useAnimatedList, useFlip, useResource } from "../hooks";
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
  fingerprint?: string;
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

function groupKey(g: DupGroup): string {
  return `${g.type}:${g.key}`;
}

function itemsFingerprint(items: DupItem[]): string {
  return items.map((i) => i.id).join(",");
}

function stripExcluded(groups: DupGroup[], excluded: Set<number>): DupGroup[] {
  if (!excluded.size) return groups;
  return groups
    .map((g) => ({
      ...g,
      items: g.items.filter((item) => !excluded.has(item.id)),
    }))
    .filter((g) => g.items.length >= 2);
}

/** Diff-merge server groups into local list; preserve object identity when unchanged. */
function mergeGroups(
  prev: DupGroup[],
  next: DupGroup[],
  excluded: Set<number> = new Set(),
): DupGroup[] {
  const cleanedNext = stripExcluded(next, excluded);
  if (!prev.length) return cleanedNext;
  const nextMap = new Map(cleanedNext.map((g) => [groupKey(g), g]));
  const prevKeys = new Set(prev.map(groupKey));
  const result: DupGroup[] = [];

  for (const g of prev) {
    const k = groupKey(g);
    const n = nextMap.get(k);
    if (!n) continue; // dropped — exit animation handles removal
    // Always drop locally excluded ids even if server is stale
    const mergedItems = n.items.filter((item) => !excluded.has(item.id));
    if (mergedItems.length < 2) continue;
    const candidate: DupGroup = { ...n, items: mergedItems };
    if (
      itemsFingerprint(g.items) === itemsFingerprint(candidate.items) &&
      g.similarity === candidate.similarity &&
      g.type === candidate.type
    ) {
      result.push(g); // same reference → no re-render
    } else {
      result.push(candidate);
    }
  }
  for (const g of cleanedNext) {
    if (!prevKeys.has(groupKey(g))) result.push(g);
  }
  return result;
}

const GroupCard = memo(
  function GroupCard({
    group,
    selected,
    onToggle,
    onSelectAll,
    onOpen,
    onKeep,
    onDelete,
    onReveal,
    onMove,
    onIgnore,
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
    onIgnore: (ids: number[]) => void;
  }) {
    const ids = group.items.map((i) => i.id);
    const allSelected = ids.every((id) => selected.has(id));
    const someSelected = ids.some((id) => selected.has(id));
    const animatedItems = useAnimatedList(group.items, (i) => i.id);
    const itemSig = animatedItems.map((e) => `${e.key}:${e.phase}`).join("|");
    const flipRef = useFlip<HTMLDivElement>(itemSig);

    return (
      <article
        className="dino-group anim-item"
        data-flip-id={`group-${group.key}`}
      >
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
            <button
              type="button"
              className="button small"
              onClick={() => onIgnore(ids)}
              title="These are not duplicates — hide this group without deleting files"
            >
              Not a duplicate
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

        <div className="dino-items" ref={flipRef}>
          {animatedItems.map(({ item, key, phase }) => {
            const isSelected = selected.has(item.id);
            return (
              <div
                key={key}
                data-flip-id={`item-${item.id}`}
                className={`dino-item anim-item anim-${phase} ${isSelected ? "selected" : ""}`}
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
  },
  (a, b) => {
    if (a.group !== b.group) return false;
    if (a.onToggle !== b.onToggle || a.onSelectAll !== b.onSelectAll)
      return false;
    if (a.onOpen !== b.onOpen || a.onKeep !== b.onKeep) return false;
    if (
      a.onDelete !== b.onDelete ||
      a.onReveal !== b.onReveal ||
      a.onMove !== b.onMove
    )
      return false;
    if (a.onIgnore !== b.onIgnore) return false;
    // Re-render only if selection among this group's items changed
    for (const item of a.group.items) {
      if (a.selected.has(item.id) !== b.selected.has(item.id)) return false;
    }
    return true;
  },
);

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
  /** Permanently hide these media ids from the Duplicates UI (until hard threshold change). */
  const [, setDeletedIds] = useState<Set<number>>(() => new Set());
  const deletedIdsRef = useRef<Set<number>>(new Set());
  const [groups, setGroups] = useState<DupGroup[]>([]);
  const [stats, setStats] = useState<{
    threshold: number;
    embedding_total: number;
    embedding_filled: number;
    embedding_remaining: number;
    group_count: number;
    item_count: number;
    dino: DuplicatesResponse["dino"] | null;
  } | null>(null);
  const [bootError, setBootError] = useState("");
  const [booting, setBooting] = useState(true);
  const [quietUpdating, setQuietUpdating] = useState(false);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const scrollLockY = useRef(0);
  const activeQs = useRef<string>("");
  const { run } = useAction();

  const qs =
    threshold != null ? `?threshold=${threshold}&limit=2000` : "?limit=2000";

  // Opt out of global lfs:refresh — that was flashing the entire tab on any app action
  const { data, error } = useResource<DuplicatesResponse>(`/duplicates${qs}`, {
    globalRefresh: false,
  });

  // Seed / replace only when the query string changes (threshold). Identity-preserving merge otherwise.
  useEffect(() => {
    if (!data) return;
    setStats({
      threshold: data.threshold,
      embedding_total: data.embedding_total,
      embedding_filled: data.embedding_filled,
      embedding_remaining: data.embedding_remaining,
      group_count: data.group_count,
      item_count: data.item_count,
      dino: data.dino,
    });
    setBootError("");
    setBooting(false);

    if (activeQs.current !== qs) {
      activeQs.current = qs;
      deletedIdsRef.current = new Set();
      setDeletedIds(new Set());
      setGroups(stripExcluded(data.groups, deletedIdsRef.current));
      setVisibleCount(12);
      return;
    }
    // Same query: granular merge — never reintroduce locally deleted media
    setGroups((prev) => mergeGroups(prev, data.groups, deletedIdsRef.current));
  }, [data, qs]);

  useEffect(() => {
    if (error) {
      setBootError(error);
      setBooting(false);
    }
  }, [error]);

  // groups is already pruned; this is the render source of truth
  const displayGroups = groups;

  const visibleGroups = useMemo(
    () => displayGroups.slice(0, visibleCount),
    [displayGroups, visibleCount],
  );
  const animatedGroups = useAnimatedList(visibleGroups, groupKey);
  const groupsFlipSig = animatedGroups
    .map((e) => `${e.key}:${e.phase}:${itemsFingerprint(e.item.items)}`)
    .join("|");
  const groupsFlipRef = useFlip<HTMLDivElement>(groupsFlipSig);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    if (visibleCount >= displayGroups.length) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisibleCount((n) => Math.min(displayGroups.length, n + 10));
        }
      },
      { root: null, rootMargin: "400px 0px", threshold: 0 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [visibleCount, displayGroups.length]);

  const remaining = stats?.embedding_remaining ?? 0;
  const filled = stats?.embedding_filled ?? 0;
  const total = stats?.embedding_total ?? 0;
  const progress = total > 0 ? Math.min(100, (filled / total) * 100) : 0;

  const preserveScroll = useCallback(() => {
    scrollLockY.current = window.scrollY;
    requestAnimationFrame(() => {
      window.scrollTo({
        top: scrollLockY.current,
        behavior: "instant" as ScrollBehavior,
      });
      requestAnimationFrame(() => {
        window.scrollTo({
          top: scrollLockY.current,
          behavior: "instant" as ScrollBehavior,
        });
      });
    });
  }, []);

  /** Quiet background fetch: merge groups, never flash loading UI. */
  const quietMerge = useCallback(async () => {
    const y = window.scrollY;
    setQuietUpdating(true);
    try {
      const next = await request<DuplicatesResponse>(`/duplicates${qs}`);
      setStats({
        threshold: next.threshold,
        embedding_total: next.embedding_total,
        embedding_filled: next.embedding_filled,
        embedding_remaining: next.embedding_remaining,
        group_count: next.group_count,
        item_count: next.item_count,
        dino: next.dino,
      });
      setGroups((prev) =>
        mergeGroups(prev, next.groups, deletedIdsRef.current),
      );
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setQuietUpdating(false);
      requestAnimationFrame(() => {
        window.scrollTo({ top: y, behavior: "instant" as ScrollBehavior });
      });
    }
  }, [qs]);

  const removeIdsLocally = useCallback(
    (ids: number[]) => {
      preserveScroll();
      const idSet = new Set(ids);
      // Tombstone so future merges cannot revive these items
      setDeletedIds((prev) => {
        const next = new Set(prev);
        ids.forEach((id) => next.add(id));
        deletedIdsRef.current = next;
        return next;
      });
      // Immediately prune groups — source of truth for the UI
      setGroups((prev) =>
        prev
          .map((g) => ({
            ...g,
            items: g.items.filter((item) => !idSet.has(item.id)),
          }))
          .filter((g) => g.items.length >= 2),
      );
      setSelected((prev) => {
        const next = new Set(prev);
        ids.forEach((id) => next.delete(id));
        return next;
      });
      setStats((s) => {
        if (!s) return s;
        // Recompute from current groups after prune (approx)
        return {
          ...s,
          item_count: Math.max(0, s.item_count - ids.length),
        };
      });
    },
    [preserveScroll],
  );

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
    const unique = [...new Set(ids)];
    // UI updates immediately — do not wait for API or full rescan
    removeIdsLocally(unique);
    setConfirmDelete(null);
    preserveScroll();
    for (const id of unique) {
      await mutate(`/media/${id}`, undefined, "DELETE");
    }
    // Re-assert prune after network (guards against any intervening merge)
    setGroups((prev) => stripExcluded(prev, deletedIdsRef.current));
    preserveScroll();
  };

  const hardRefresh = useCallback(() => {
    void quietMerge();
  }, [quietMerge]);

  const doIgnore = useCallback(
    async (ids: number[]) => {
      if (ids.length < 2) return;
      preserveScroll();
      // Remove this group from local state immediately (exit animation)
      setGroups((prev) =>
        prev.filter((g) => {
          const gids = g.items.map((i) => i.id).sort((a, b) => a - b);
          const target = [...ids].sort((a, b) => a - b);
          if (gids.length !== target.length) return true;
          return gids.some((id, idx) => id !== target[idx]);
        }),
      );
      setSelected((prev) => {
        const next = new Set(prev);
        ids.forEach((id) => next.delete(id));
        return next;
      });
      setStats((s) =>
        s
          ? {
              ...s,
              group_count: Math.max(0, s.group_count - 1),
              item_count: Math.max(0, s.item_count - ids.length),
            }
          : s,
      );
      try {
        await mutate("/duplicates/ignore", { media_ids: ids });
      } catch (e) {
        setMsg(e instanceof Error ? e.message : String(e));
      }
      preserveScroll();
    },
    [preserveScroll],
  );

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
    const ids = [...moveIds];
    await run(
      async () => {
        await mutate("/media/move", {
          media_ids: ids,
          destination: moveDest.trim(),
        });
        removeIdsLocally(ids);
        setMoveIds(null);
        setMoveDest("");
        setGroups((prev) => stripExcluded(prev, deletedIdsRef.current));
        preserveScroll();
      },
      `Moved ${ids.length} file(s)`,
      false,
    );
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
        setStats((s) =>
          s
            ? {
                ...s,
                embedding_filled: s.embedding_total - res.remaining,
                embedding_remaining: res.remaining,
                dino: res.dino
                  ? {
                      available: res.dino.available,
                      device: s.dino?.device ?? null,
                      dim: s.dino?.dim ?? 0,
                      error: res.dino.error,
                    }
                  : s.dino,
              }
            : s,
        );
        setMsg(
          `Embedded ${totalFilled} media… ${res.remaining} remaining` +
            (res.dino && !res.dino.available
              ? ` (DINOv2: ${res.dino.error || "unavailable"})`
              : ""),
        );
        if (res.remaining === 0 || res.filled === 0) break;
      }
      // Merge any newly discovered groups without wiping the list
      await quietMerge();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBackfilling(false);
    }
  };

  const effectiveThreshold = threshold ?? stats?.threshold ?? 0.92;

  return (
    <>
      <PageHeader
        eyebrow="FIND COPIES FAST"
        title="Duplicates"
        description="Exact copies and visually similar photos/videos via DINOv2. Nothing is deleted automatically."
      />

      {bootError && <ErrorNotice error={bootError} retry={hardRefresh} />}

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
              onClick={() => hardRefresh()}
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

        {stats && (
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
                <strong>{number(stats.group_count)}</strong> groups ·{" "}
                {number(stats.item_count)} files
                {quietUpdating ? " · updating…" : ""}
              </span>
              <span className="dino-device">
                {stats.dino?.available
                  ? `DINOv2 · ${stats.dino.device || "cpu"}`
                  : stats.dino?.error
                    ? `DINOv2 unavailable`
                    : "DINOv2"}
              </span>
            </div>
          </div>
        )}
        {msg && <p className="dino-status-msg">{msg}</p>}
      </section>

      {booting && !groups.length && <Loading label="Scanning for duplicates" />}

      {!booting && displayGroups.length === 0 && (
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

      <div className="dino-groups" ref={groupsFlipRef}>
        {animatedGroups.map(({ item: g, key, phase }) => (
          <div
            key={key}
            data-flip-id={`group-${g.type}-${g.key}`}
            className={`anim-item anim-${phase}`}
          >
            <GroupCard
              group={g}
              selected={selected}
              onToggle={toggle}
              onSelectAll={selectAll}
              onOpen={setViewer}
              onKeep={doKeep}
              onDelete={(ids) => setConfirmDelete(ids)}
              onReveal={(id) => void doReveal(id)}
              onMove={setMoveIds}
              onIgnore={(ids) => void doIgnore(ids)}
            />
          </div>
        ))}
        {visibleCount < displayGroups.length && (
          <div ref={sentinelRef} className="dino-scroll-sentinel" aria-hidden>
            <span className="muted small-text">
              Showing {visibleCount} of {displayGroups.length} groups…
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
        refresh={false}
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
