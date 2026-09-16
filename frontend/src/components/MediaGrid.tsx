import {
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from "react";
import {
  batch,
  bytes,
  dateLabel,
  exportZip,
  mutate,
  number,
  queryString,
  timeLabel,
} from "../api";
import { useAction } from "../context";
import { useDebounced, useResource, useSelection } from "../hooks";
import type { Media, MediaFilters, Page } from "../types";
import { Icon } from "./Icon";
import { MediaViewer } from "./MediaViewer";
import { PersonPicker, PersonToken } from "./PersonPicker";
import {
  Badge,
  ConfirmDialog,
  Dialog,
  Empty,
  ErrorNotice,
  Loading,
  Pagination,
  Thumbnail,
} from "./ui";

function VideoHoverPreview({ id, name }: { id: number; name: string }) {
  const [hover, setHover] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    if (hover) {
      el.currentTime = 0;
      void el.play().catch(() => {});
    } else {
      el.pause();
    }
  }, [hover]);

  return (
    <div
      className="video-hover-preview"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <Thumbnail src={`/api/media/${id}/thumbnail`} alt={name} icon="video" />
      {hover && (
        <video
          ref={videoRef}
          className="video-hover-player"
          src={`/api/media/${id}/file`}
          muted
          playsInline
          loop
          preload="metadata"
        />
      )}
    </div>
  );
}

function getAspectRatio(item: Media): number {
  const w = item.width;
  const h = item.height;
  if (w && h && w > 0 && h > 0) return w / h;
  return item.kind === "video" ? 16 / 9 : 3 / 4;
}

export function MediaGrid({
  items,
  selected,
  onSelect,
  onOpen,
  layout = "grid",
}: {
  items: Media[];
  selected?: Set<number>;
  onSelect?: (
    id: number,
    event?: ReactMouseEvent,
    mode?: "toggle" | "range" | "paint",
  ) => void;
  onOpen: (id: number) => void;
  layout?: "grid" | "mosaic";
}) {
  const paintRef = useRef(false);
  const mosaic = layout === "mosaic";

  useEffect(() => {
    const up = () => {
      paintRef.current = false;
    };
    window.addEventListener("mouseup", up);
    return () => window.removeEventListener("mouseup", up);
  }, []);

  return (
    <div className={mosaic ? "media-grid media-mosaic" : "media-grid"}>
      {items.map((item) => {
        const aspectRatio = getAspectRatio(item);

        return (
          <article
            className={`media-card ${selected?.has(item.id) ? "selected" : ""}`}
            key={item.id}
            onMouseEnter={() => {
              if (paintRef.current && onSelect)
                onSelect(item.id, undefined, "paint");
            }}
            style={
              mosaic
                ? {
                    flexGrow: aspectRatio,
                    width: `${aspectRatio * 200}px`,
                  }
                : undefined
            }
          >
            <div
              className="media-image"
              style={mosaic ? { aspectRatio: String(aspectRatio) } : undefined}
            >
              <button
                className="image-button"
                onClick={() => onOpen(item.id)}
                aria-label={`Open ${item.name}`}
              >
                {item.kind === "video" ? (
                  <VideoHoverPreview id={item.id} name={item.name} />
                ) : (
                  <Thumbnail
                    src={`/api/media/${item.id}/thumbnail`}
                    alt={item.name}
                    icon="photo"
                  />
                )}
              </button>
              {onSelect && (
                <label
                  className="select-check"
                  onMouseDown={(event) => {
                    if (event.button === 0 && !event.shiftKey)
                      paintRef.current = true;
                  }}
                >
                  <input
                    type="checkbox"
                    checked={selected?.has(item.id) ?? false}
                    onChange={(event) => {
                      const native = event.nativeEvent as MouseEvent;
                      if (native.shiftKey)
                        onSelect(
                          item.id,
                          event as unknown as ReactMouseEvent,
                          "range",
                        );
                      else
                        onSelect(
                          item.id,
                          event as unknown as ReactMouseEvent,
                          "toggle",
                        );
                    }}
                    onClick={(event) => event.stopPropagation()}
                    aria-label={`Select ${item.name}`}
                  />
                </label>
              )}
              {item.kind === "video" && (
                <span className="media-duration">
                  <Icon name="play" size={12} />
                  {timeLabel(item.duration)}
                </span>
              )}
              {item.size > 0 && (
                <span className="media-size-badge" title="File size">
                  {bytes(item.size)}
                </span>
              )}
              {(item.missing ||
                item.deleted_at ||
                item.status === "failed") && (
                <span className="media-warning">
                  <Icon name="alert" size={13} />
                  {item.missing
                    ? "Missing"
                    : item.deleted_at
                      ? "Deleted"
                      : "Failed"}
                </span>
              )}
              {!!item.face_count && (
                <span className="face-count">
                  <Icon name="people" size={12} />
                  {item.face_count}
                </span>
              )}
              {mosaic && (
                <div className="mosaic-hover-caption">
                  <strong title={item.name}>{item.name}</strong>
                  <span className="media-meta-line">
                    {dateLabel(item.captured_at)}
                    {item.size > 0 ? (
                      <em className="media-size-text"> · {bytes(item.size)}</em>
                    ) : null}
                    {item.width && item.height ? (
                      <span className="media-dims">
                        {" "}
                        · {item.width}×{item.height}
                      </span>
                    ) : null}
                  </span>
                  {item.people.length > 0 && (
                    <p
                      title={item.people
                        .map((person) => person.display_name)
                        .join(", ")}
                    >
                      {item.people
                        .map((person) => person.display_name)
                        .join(", ")}
                    </p>
                  )}
                </div>
              )}
            </div>
            {!mosaic && (
              <div className="media-caption">
                <strong title={item.name}>{item.name}</strong>
                <span className="media-meta-line">
                  {dateLabel(item.captured_at)}
                  {item.size > 0 ? (
                    <em className="media-size-text">{bytes(item.size)}</em>
                  ) : null}
                  {item.width && item.height ? (
                    <span className="media-dims">
                      {item.width}×{item.height}
                    </span>
                  ) : null}
                </span>
                {item.people.length > 0 && (
                  <p
                    title={item.people
                      .map((person) => person.display_name)
                      .join(", ")}
                  >
                    {item.people
                      .map((person) => person.display_name)
                      .join(", ")}
                  </p>
                )}
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}

const MEDIA_SORT_OPTIONS = [
  { value: "date", label: "Newest first" },
  { value: "size_desc", label: "Size · largest" },
  { value: "size_asc", label: "Size · smallest" },
  { value: "name", label: "Name A–Z" },
] as const;

export function MediaCollection({
  filters = {},
  tools = true,
  personId,
  emptyTitle = "Nothing here just yet",
  emptyDescription = "Add a library in Settings and run a scan. Your indexed media will appear here.",
  purgeOriginals = false,
}: {
  filters?: MediaFilters;
  tools?: boolean;
  personId?: number;
  emptyTitle?: string;
  emptyDescription?: string;
  purgeOriginals?: boolean;
}) {
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const search = useDebounced(q);
  const [excluded, setExcluded] = useState(false);
  const [deleted, setDeleted] = useState(false);
  const [sort, setSort] =
    useState<(typeof MEDIA_SORT_OPTIONS)[number]["value"]>("date");
  const [excludePeople, setExcludePeople] = useState<number[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [layout, setLayout] = useState<"grid" | "mosaic">("mosaic");

  const actualFilters = {
    ...filters,
    sort,
    ...(excludePeople.length ? { exclude_people: excludePeople } : {}),
    ...(tools
      ? purgeOriginals
        ? { q: search, excluded: false, deleted: false }
        : { q: search, excluded, deleted }
      : { excluded: false, deleted: false }),
  };

  const filterKey = queryString(actualFilters);
  useEffect(() => setPage(1), [filterKey]);

  const path = `/media?${filterKey}&page=${page}&limit=60`;
  const resource = useResource<Page<Media>>(path);
  const selection = useSelection(filterKey);
  const action = useAction();
  const [viewer, setViewer] = useState<number | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [purgeOpen, setPurgeOpen] = useState(false);

  const items = resource.data?.items ?? [];
  const lastAnchor = useRef<number | null>(null);

  const handleSelect = (
    id: number,
    _event?: ReactMouseEvent,
    mode: "toggle" | "range" | "paint" = "toggle",
  ) => {
    const order = items.map((item) => item.id);
    if (mode === "range" && lastAnchor.current != null) {
      const a = order.indexOf(lastAnchor.current);
      const b = order.indexOf(id);
      if (a >= 0 && b >= 0) {
        const [lo, hi] = a < b ? [a, b] : [b, a];
        selection.addRange(order.slice(lo, hi + 1));
        return;
      }
    }
    if (mode === "paint") {
      selection.addRange([id]);
      return;
    }
    lastAnchor.current = id;
    selection.toggle(id);
  };

  const ids = Array.from(selection.selected);
  const selectedCount = ids.length;
  const pageSelected = items.filter((item) => selection.selected.has(item.id));

  const run = async (fn: () => Promise<unknown>, success: string) => {
    if (await action.run(fn, success)) selection.clear();
  };

  const purgeDialog = (
    <ConfirmDialog
      open={purgeOriginals ? deleteOpen : purgeOpen}
      onClose={() => {
        setDeleteOpen(false);
        setPurgeOpen(false);
      }}
      title={`Permanently delete ${selectedCount} original file(s)?`}
      description="WARNING: This permanently deletes the original photos/videos from disk. They cannot be recovered from this app. Index records will also be removed. Type DELETE to confirm."
      label="Delete originals forever"
      confirmation="DELETE"
      onConfirm={async () => {
        await mutate("/media/purge", { media_ids: ids, confirm: "DELETE" });
        selection.clear();
      }}
      success="Selected original files deleted from disk and removed from the index."
    />
  );

  return (
    <section className="collection" aria-label="Media collection">
      {tools && (
        <div className="collection-filters">
          <label className="input-icon">
            <Icon name="search" size={18} />
            <input
              type="search"
              aria-label="Filter media by filename"
              placeholder="Filter by filename..."
              value={q}
              onChange={(event) => setQ(event.target.value)}
            />
          </label>
          <label className="field-inline">
            <span className="sr-only">Sort media</span>
            <select
              aria-label="Sort media"
              value={sort}
              onChange={(event) => {
                setSort(event.target.value as typeof sort);
                setPage(1);
              }}
            >
              {MEDIA_SORT_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </label>
          <div className="segmented" role="group" aria-label="Gallery layout">
            <button
              type="button"
              aria-pressed={layout === "mosaic"}
              onClick={() => setLayout("mosaic")}
            >
              Mosaic
            </button>
            <button
              type="button"
              aria-pressed={layout === "grid"}
              onClick={() => setLayout("grid")}
            >
              Grid
            </button>
          </div>
          {!purgeOriginals && !filters.no_faces && (
            <button
              type="button"
              className="button small"
              onClick={() => setPickerOpen(true)}
            >
              <Icon name="hidden" size={16} />
              Hide people
              {excludePeople.length ? ` (${excludePeople.length})` : ""}
            </button>
          )}
          {!purgeOriginals && (
            <>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={excluded}
                  onChange={(event) => setExcluded(event.target.checked)}
                />
                Include excluded matches
              </label>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={deleted}
                  onChange={(event) => setDeleted(event.target.checked)}
                />
                Deleted media
              </label>
            </>
          )}
        </div>
      )}
      {!!excludePeople.length && (
        <div className="collection-bar" style={{ marginTop: 0 }}>
          <div className="inline-actions" style={{ flexWrap: "wrap", gap: 8 }}>
            <span className="muted small-text">Hiding media with:</span>
            {excludePeople.map((id) => (
              <PersonToken
                key={id}
                id={id}
                onRemove={() =>
                  setExcludePeople((list) => list.filter((x) => x !== id))
                }
              />
            ))}
            <button
              type="button"
              className="button ghost small"
              onClick={() => setExcludePeople([])}
            >
              Clear all
            </button>
          </div>
        </div>
      )}
      <Dialog
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        title="Hide people from this view"
      >
        <div className="dialog-body">
          <p className="muted small-text">
            Select people to exclude. Photos/videos that contain any of them
            will be hidden — everyone else still appears.
          </p>
          <PersonPicker
            excluded={excludePeople}
            label="Add someone to hide"
            onSelect={(person) => {
              setExcludePeople((list) =>
                list.includes(person.id) ? list : [...list, person.id],
              );
              setPage(1);
            }}
          />
        </div>
        <div className="dialog-footer">
          <button
            type="button"
            className="button primary"
            onClick={() => setPickerOpen(false)}
          >
            Done
          </button>
        </div>
      </Dialog>
      <div className="collection-bar">
        <div className="inline-actions">
          {!!items.length && (
            <label className="check-label">
              <input
                type="checkbox"
                checked={
                  items.length > 0 &&
                  items.every((item) => selection.selected.has(item.id))
                }
                onChange={() => selection.all(items.map((item) => item.id))}
              />
              Select page
            </label>
          )}
          <span className="muted small-text">
            {resource.data
              ? `${number(resource.data.total)} results`
              : "Your local collection"}
            {selectedCount > 0 ? ` · ${number(selectedCount)} selected` : ""}
          </span>
          {resource.loading && !!resource.data && <Badge>Refreshing</Badge>}
        </div>
        <button
          className="button small"
          disabled={!resource.data?.total || action.busy || resource.loading}
          onClick={() =>
            void action.run(
              () => exportZip("/export", { filters: actualFilters }),
              "Matching originals and manifest downloaded.",
              false,
            )
          }
        >
          <Icon name="download" size={16} />
          {action.busy ? "Working..." : "Export results"}
        </button>
      </div>
      {selectedCount > 0 && (
        <div className="selection-toolbar">
          <strong>{selectedCount} selected</strong>
          <div className="inline-actions">
            <button
              className="button small"
              disabled={action.busy}
              onClick={() =>
                void action.run(
                  () => exportZip("/export", { media_ids: ids }),
                  "Selected media downloaded.",
                  false,
                )
              }
            >
              <Icon name="download" size={16} />
              Export
            </button>
            {personId && (
              <button
                className="button small"
                disabled={action.busy}
                onClick={() =>
                  void run(
                    () =>
                      batch(ids, (media_id) =>
                        mutate("/exclusions", {
                          person_id: personId,
                          media_id,
                        }),
                      ),
                    "Selected media excluded for this person.",
                  )
                }
              >
                <Icon name="hidden" size={16} />
                Exclude for person
              </button>
            )}
            {!purgeOriginals &&
            pageSelected.some((item) => !!item.deleted_at) ? (
              <button
                className="button small"
                disabled={action.busy}
                onClick={() =>
                  void run(
                    () =>
                      batch(
                        pageSelected.filter((item) => !!item.deleted_at),
                        (item) => mutate(`/media/${item.id}/restore`),
                      ),
                    "Media restored.",
                  )
                }
              >
                <Icon name="restore" size={16} />
                Restore
              </button>
            ) : (
              <>
                {!purgeOriginals && (
                  <button
                    className="button small danger-text"
                    disabled={action.busy}
                    onClick={() => setDeleteOpen(true)}
                  >
                    <Icon name="trash" size={16} />
                    Delete from index
                  </button>
                )}
                <button
                  className="button small danger-text"
                  disabled={action.busy}
                  onClick={() =>
                    purgeOriginals ? setDeleteOpen(true) : setPurgeOpen(true)
                  }
                >
                  <Icon name="trash" size={16} />
                  Delete originals forever
                </button>
              </>
            )}
            <button
              className="button ghost small"
              onClick={selection.clear}
              disabled={action.busy}
            >
              Clear selection
            </button>
          </div>
        </div>
      )}
      <ErrorNotice error={resource.error} retry={resource.reload} />
      {resource.loading && !resource.data ? (
        <Loading />
      ) : resource.data && !items.length ? (
        <Empty
          icon={filters.kind === "video" ? "video" : "photo"}
          title={emptyTitle}
          description={emptyDescription}
        />
      ) : (
        <MediaGrid
          items={items}
          selected={selection.selected}
          onSelect={handleSelect}
          onOpen={setViewer}
          layout={layout}
        />
      )}
      {tools && (
        <p className="muted small-text" style={{ marginTop: 8 }}>
          Tip: hover a video to preview · Shift+click checkboxes for a range ·
          drag across checkboxes to paint-select
        </p>
      )}
      {resource.data && (
        <Pagination
          page={page}
          total={resource.data.total}
          limit={60}
          onPage={setPage}
        />
      )}
      {viewer !== null && (
        <MediaViewer
          id={viewer}
          ids={items.map((item) => item.id)}
          onClose={() => setViewer(null)}
        />
      )}
      {purgeDialog}
      {!purgeOriginals && (
        <ConfirmDialog
          open={deleteOpen}
          onClose={() => setDeleteOpen(false)}
          title={`Delete ${selectedCount} media records?`}
          description="This removes the selected media from the active index only. Your original files will not be deleted. Use the deleted media filter to restore records."
          label="Delete from index"
          onConfirm={async () => {
            await batch(ids, (mediaId) =>
              mutate(`/media/${mediaId}`, undefined, "DELETE"),
            );
            selection.clear();
          }}
          success="Selected media deleted from the index."
        />
      )}
    </section>
  );
}
