import { Button } from "@/components/primitives/button";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { useWindowVirtualizer } from "@tanstack/react-virtual";
import { Link } from "react-router-dom";
import { number, timeLabel } from "../api";
import { useResource } from "../hooks";
import { useLqip } from "../lqip";
import { useT } from "../i18n";
import type { Media } from "../types";
import { Icon } from "../components/Icon";
import { MediaViewer } from "../components/MediaViewer";
import { Empty, ErrorNotice, GallerySkeleton, PageHeader, Thumbnail } from "../components/ui";
import { gridGeometry } from "../virtual/geometry";
import { useWindowedPages } from "../virtual/useWindowedPages";
import { groupDays, layoutRows, rowAt, scrubMarks, sectionLabel, type DayCount } from "../timeline/sections";

interface TimelineData { days: DayCount[]; total: number; undated: number; guessed: number }

const HEADER = 52;
const GAP = 6;

export function Timeline({ photos = false }: { photos?: boolean }) {
  const t = useT();
  const [kind, setKind] = useState<"" | "photo" | "video">("");
  const data = useResource<TimelineData>(`/timeline${kind ? `?kind=${kind}` : ""}`);
  const windowed = useWindowedPages<Media>(`/media?sort=date${kind ? `&kind=${kind}` : ""}`, 120);
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(1000);
  const [offset, setOffset] = useState(0);
  const [viewer, setViewer] = useState<{ id: number; index: number } | null>(null);
  const [scrollPosition, setScrollPosition] = useState(0);
  const current = Math.max(0, scrollPosition - offset);

  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measure = () => { setWidth(node.clientWidth || 1000); setOffset(node.getBoundingClientRect().top + window.scrollY); };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [data.data]);

  const geometry = gridGeometry(width, 0, width < 600 ? 104 : 150, GAP, 1, 0);
  const sections = useMemo(() => groupDays([...(data.data?.days ?? []),
    ...(data.data?.undated ? [["", data.data.undated] as DayCount] : [])]), [data.data]);
  const { rows, height } = useMemo(() => layoutRows(sections, geometry.columns, geometry.rowHeight, HEADER),
    [sections, geometry.columns, geometry.rowHeight]);
  const marks = useMemo(() => scrubMarks(sections, rows), [sections, rows]);

  const virtualizer = useWindowVirtualizer({
    count: rows.length,
    estimateSize: (i) => rows[i]?.height ?? geometry.rowHeight,
    overscan: 6,
    scrollMargin: offset,
    initialRect: { width: 1024, height: 900 },
  });
  useEffect(() => { virtualizer.measure(); }, [rows, virtualizer]);
  const visible = virtualizer.getVirtualItems();

  const { onRange } = windowed;
  useEffect(() => {
    const itemRows = visible.map((v) => rows[v.index]).filter((r) => r?.kind === "items");
    if (!itemRows.length) return;
    const first = itemRows[0], last = itemRows[itemRows.length - 1];
    if (first.kind === "items" && last.kind === "items") onRange(first.first, last.first + last.count - 1);
  }, [visible, rows, onRange]);

  // Sticky header + scrubber position follow the scroll position.
  useEffect(() => {
    let frame = 0;
    const onScroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => setScrollPosition(window.scrollY));
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => { cancelAnimationFrame(frame); window.removeEventListener("scroll", onScroll); };
  }, [offset]);
  const currentRow = rows.length ? rows[rowAt(rows, current + 1)] : undefined;
  const currentSection = currentRow ? sections[currentRow.section] : undefined;
  // The floating date only appears once its section's own header has scrolled away.
  const headerTop = useMemo(() => {
    const tops = new Map<number, number>();
    rows.forEach((row) => { if (row.kind === "header") tops.set(row.section, row.top); });
    return tops;
  }, [rows]);
  const showSticky = currentRow !== undefined && current > (headerTop.get(currentRow.section) ?? 0) + HEADER;

  const scrollToY = useCallback((y: number) => {
    window.scrollTo({ top: offset + Math.max(0, Math.min(height, y)), behavior: "instant" as ScrollBehavior });
  }, [offset, height]);

  // Request blur previews for mounted rows, rather than decoding every prefetched page.
  const placeholders = useLqip(visible.flatMap(({ index }) => {
    const row = rows[index];
    if (row?.kind !== "items") return [];
    return Array.from({ length: row.count }, (_, i) => windowed.getItem(row.first + i)?.id)
      .filter((id): id is number => id !== undefined);
  }));

  return (
    <>
      <PageHeader eyebrow={t("timeline.eyebrow")} title={t(photos ? "common.photos" : "timeline.title")}
        description={t("timeline.description")} actions={<Link className="button small" to="/browse">
          <Icon name="filter" size={16} />{t("photos.organize")}</Link>} />
      <div className="collection-filters">
        <div className="segmented" role="group" aria-label={t("timeline.kind")}>
          {(["", "photo", "video"] as const).map((value) => (
            <Button key={value || "all"} type="button" aria-pressed={kind === value} onClick={() => setKind(value)}>
              {t(value === "" ? "common.all" : value === "photo" ? "common.photos" : "common.videos")}
            </Button>
          ))}
        </div>
        {data.data && (
          <span className="muted small-text">
            {t("timeline.count", { count: number(data.data.total) })}
            {data.data.guessed > 0 && ` · ${t("timeline.guessed", { count: number(data.data.guessed) })}`}
          </span>
        )}
      </div>
      <ErrorNotice error={data.error || windowed.error} retry={() => { data.reload(); windowed.reload(); }} />
      {data.loading ? <GallerySkeleton label={t("timeline.loading")} /> : !rows.length ? (
        <Empty icon="photo" title={t("timeline.emptyTitle")} description={t("timeline.emptyDescription")} />
      ) : (
        <div className="timeline">
          {currentSection && showSticky && (
            <div className="timeline-sticky" aria-hidden="true">{sectionLabel(currentSection)}</div>
          )}
          <div ref={ref} className="timeline-body" style={{ height }} role="region" aria-label={t("timeline.title")} aria-busy={windowed.refreshing}>
            {visible.map((v) => {
              const row = rows[v.index];
              const style = { position: "absolute" as const, top: 0, left: 0, right: 0, height: row.height,
                transform: `translateY(${v.start - virtualizer.options.scrollMargin}px)` };
              if (row.kind === "header") {
                const section = sections[row.section];
                return (
                  <h2 key={v.key} className="timeline-header" style={style}>
                    <span>{sectionLabel(section)}</span>
                    <span className="muted small-text">{number(section.count)}</span>
                  </h2>
                );
              }
              return (
                <div key={v.key} className="timeline-row" style={{ ...style, height: row.height - GAP,
                  gridTemplateColumns: `repeat(${geometry.columns}, minmax(0, 1fr))`, gap: GAP }}>
                  {Array.from({ length: row.count }, (_, i) => {
                    const item = windowed.getItem(row.first + i);
                    return item ? (
                      <Button key={item.id} className="timeline-tile" onClick={() => setViewer({ id: item.id, index: row.first + i })}
                        aria-label={`${item.name}${item.captured_at ? `, ${new Date(item.captured_at).toLocaleString()}` : ""}`}>
                        <Thumbnail src={`/api/media/${item.id}/thumbnail?size=384${item.edit_version ? `&v=${item.edit_version}` : ""}`} alt="" eager={row.first + i < geometry.columns} placeholder={placeholders[item.id]} />
                        {item.kind === "video" && <span className="media-duration"><Icon name="play" size={11} />{timeLabel(item.duration)}</span>}
                        {(item.missing || item.status === "failed" || item.status === "pending") &&
                          <span className="media-warning"><Icon name="alert" size={13} />{t(item.missing ? "photos.missing" : item.status === "failed" ? "photos.failed" : "photos.indexing")}</span>}
                      </Button>
                    ) : <div key={i} className="cell-skeleton" aria-hidden="true" />;
                  })}
                </div>
              );
            })}
          </div>
          <Scrubber top={Math.max(0, offset - scrollPosition)} marks={marks} height={height} position={current} label={currentSection ? sectionLabel(currentSection) : ""}
            onScrub={scrollToY} ariaLabel={t("timeline.scrubber")} />
        </div>
      )}
      {viewer !== null && <MediaViewer id={viewer.id} onClose={() => setViewer(null)} paging={{ index: viewer.index,
        total: windowed.count, onStep: async delta => {
          const item = await windowed.getItemAt(viewer.index + delta);
          if (item) setViewer({ id: item.id, index: viewer.index + delta });
        } }} />}
    </>
  );
}

function Scrubber({ top, marks, height, position, label, onScrub, ariaLabel }: {
  top: number;
  marks: ReturnType<typeof scrubMarks>; height: number; position: number; label: string;
  onScrub: (y: number) => void; ariaLabel: string;
}) {
  const rail = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const [railHeight, setRailHeight] = useState(0);
  useEffect(() => {
    if (!rail.current) return;
    const observer = new ResizeObserver(([entry]) => setRailHeight(entry.contentRect.height));
    observer.observe(rail.current);
    return () => observer.disconnect();
  }, []);
  const years = marks.filter((m) => m.month === undefined);
  const months = marks.filter((m) => m.month !== undefined);
  // Uneven library dates can put many years in a few pixels. Keep navigation
  // anchors intact, but show only labels with enough room on the visible rail.
  const visibleYears = railHeight && height && years.length
    ? (years.length > 1 ? [years[0], years[years.length - 1]] : [years[0]]) : [];
  if (railHeight && height && years.length > 2) {
    let previous = years[0].top;
    const last = years[years.length - 1].top;
    for (const mark of years.slice(1, -1)) {
      if ((mark.top - previous) / height * railHeight >= 22 && (last - mark.top) / height * railHeight >= 22) {
        visibleYears.splice(visibleYears.length - 1, 0, mark);
        previous = mark.top;
      }
    }
  }
  const ratio = height ? Math.min(1, position / height) : 0;
  const fromPointer = (event: PointerEvent) => {
    const box = rail.current?.getBoundingClientRect();
    if (!box) return;
    onScrub(((event.clientY - box.top) / box.height) * height);
  };
  const step = (list: typeof marks, direction: 1 | -1) => {
    const index = list.findIndex((m) => m.top > position + 1);
    const next = direction > 0 ? list[index === -1 ? list.length - 1 : index] : [...list].reverse().find((m) => m.top < position - 1);
    if (next) onScrub(next.top);
  };
  const onKey = (event: KeyboardEvent) => {
    const map: Record<string, () => void> = {
      ArrowDown: () => step(months, 1), ArrowUp: () => step(months, -1),
      PageDown: () => step(years, 1), PageUp: () => step(years, -1),
      Home: () => onScrub(0), End: () => onScrub(height),
    };
    if (map[event.key]) { event.preventDefault(); map[event.key](); }
  };
  return (
    <div className="timeline-scrubber" style={{ top: `max(calc(var(--topbar-height) + 20px), ${top}px)` }}>
      <div ref={rail} className="scrubber-rail" role="slider" tabIndex={0} aria-label={ariaLabel}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(ratio * 100)} aria-valuetext={label}
        aria-orientation="vertical" onKeyDown={onKey}
        onPointerDown={(e) => { dragging.current = true; e.currentTarget.setPointerCapture(e.pointerId); fromPointer(e); }}
        onPointerMove={(e) => { if (dragging.current) fromPointer(e); }}
        onPointerUp={() => { dragging.current = false; }}
        onPointerCancel={() => { dragging.current = false; }}>
        {visibleYears.filter(mark => Math.abs((mark.top / height - ratio) * railHeight) >= 12).map((mark) => (
          <span key={mark.year} className="scrubber-year" style={{ top: `${(mark.top / height) * 100}%` }}>{mark.label}</span>
        ))}
        <span className="scrubber-thumb" style={{ top: `${ratio * 100}%` }}>
          <span className="scrubber-label">{label}</span>
        </span>
      </div>
    </div>
  );
}
