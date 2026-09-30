import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { useWindowVirtualizer } from '@tanstack/react-virtual';
import { gridGeometry, moveIndex } from './geometry';

export interface VirtualGridProps<T> {
  count: number;
  getItem: (index: number) => T | undefined;
  renderCell: (item: T | undefined, index: number, state: { active: boolean; tabIndex: number }) => ReactNode;
  /** Called with the visible item range so the data layer can fetch those pages. */
  onRange?: (start: number, end: number) => void;
  label: string;
  minCell?: number;
  gap?: number;
  /** Cell height = width * aspect + extra (caption). */
  aspect?: number;
  extra?: number;
  className?: string;
  onOpen?: (index: number, item: T) => void;
  onToggle?: (index: number, item: T, range: boolean) => void;
  onSelectAll?: () => void;
  onEscape?: () => void;
  onDelete?: () => void;
  /** Server-rendered/test fallback: how many rows to render without a measured viewport. */
  initialRows?: number;
}

/**
 * Window-scrolled virtual grid with fixed row heights (no layout shift while
 * pages stream in) and ARIA grid semantics: one tab stop, arrow/Page/Home/End
 * navigation, Space/Shift+Space to select, Enter to open, Ctrl/Cmd+A, Escape,
 * Delete. Only the rows in view (+overscan) exist in the DOM.
 */
export function VirtualGrid<T>({
  count, getItem, renderCell, onRange, label, minCell = 180, gap = 14, aspect = 1, extra = 0, className = '',
  onOpen, onToggle, onSelectAll, onEscape, onDelete, initialRows = 3,
}: VirtualGridProps<T>) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [offset, setOffset] = useState(0);
  const [active, setActive] = useState(0);
  const focusAfterScroll = useRef<number | null>(null);

  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measure = () => {
      setWidth(node.clientWidth);
      setOffset(node.getBoundingClientRect().top + window.scrollY);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const geometry = gridGeometry(width || minCell * 4 + gap * 3, count, minCell, gap, aspect, extra);
  const virtualizer = useWindowVirtualizer({
    count: geometry.rows,
    estimateSize: () => geometry.rowHeight,
    overscan: 3,
    scrollMargin: offset,
    initialRect: { width: 1024, height: geometry.rowHeight * initialRows },
  });
  useEffect(() => { virtualizer.measure(); }, [geometry.rowHeight, geometry.columns, virtualizer]);

  const rows = virtualizer.getVirtualItems();
  const firstRow = rows[0]?.index ?? 0;
  const lastRow = rows[rows.length - 1]?.index ?? 0;
  useEffect(() => {
    onRange?.(firstRow * geometry.columns, (lastRow + 1) * geometry.columns - 1);
  }, [firstRow, lastRow, geometry.columns, onRange]);

  const activeIndex = Math.min(active, Math.max(0, count - 1));
  const focusCell = useCallback((index: number) => {
    const cell = ref.current?.querySelector<HTMLElement>(`[data-index="${index}"]`);
    if (cell) { cell.focus({ preventScroll: true }); focusAfterScroll.current = null; }
    else focusAfterScroll.current = index;
  }, []);
  useEffect(() => {
    if (focusAfterScroll.current !== null) focusCell(focusAfterScroll.current);
  });

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target instanceof HTMLElement && event.target.closest('input,button,a,select,textarea') && !event.target.matches('[role=gridcell]')) return;
    const item = getItem(activeIndex);
    const mod = event.metaKey || event.ctrlKey;
    if (mod && event.key.toLowerCase() === 'a' && onSelectAll) { event.preventDefault(); onSelectAll(); return; }
    if (event.key === 'Escape' && onEscape) { onEscape(); return; }
    if ((event.key === 'Delete' || event.key === 'Backspace') && onDelete) { event.preventDefault(); onDelete(); return; }
    if (event.key === 'Enter' && item && onOpen) { event.preventDefault(); onOpen(activeIndex, item); return; }
    if (event.key === ' ' && item && onToggle) { event.preventDefault(); onToggle(activeIndex, item, event.shiftKey); return; }
    const pageRows = Math.max(1, Math.floor(window.innerHeight / geometry.rowHeight));
    const next = moveIndex(activeIndex, event.key, geometry.columns, count, pageRows);
    if (next === null) return;
    event.preventDefault();
    setActive(next);
    virtualizer.scrollToIndex(Math.floor(next / geometry.columns), { align: 'auto' });
    focusCell(next);
    if (event.shiftKey && onToggle) {
      const target = getItem(next);
      if (target) onToggle(next, target, true);
    }
  };

  return (
    <div ref={ref} className={`virtual-grid ${className}`} role="grid" aria-label={label} aria-rowcount={geometry.rows}
      aria-colcount={geometry.columns} onKeyDown={onKeyDown}
      onFocus={event => {
        const index = Number((event.target as HTMLElement).dataset?.index);
        if (Number.isFinite(index)) setActive(index);
      }}
      style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
      {rows.map(row => (
        <div key={row.key} role="row" aria-rowindex={row.index + 1} className="virtual-row"
          style={{
            position: 'absolute', top: 0, left: 0, right: 0, height: geometry.rowHeight - gap,
            transform: `translateY(${row.start - virtualizer.options.scrollMargin}px)`,
            display: 'grid', gridTemplateColumns: `repeat(${geometry.columns}, minmax(0, 1fr))`, gap,
          }}>
          {Array.from({ length: geometry.columns }, (_, column) => {
            const index = row.index * geometry.columns + column;
            if (index >= count) return <div key={column} role="presentation" />;
            const isActive = index === activeIndex;
            return (
              <div key={column} role="gridcell" aria-colindex={column + 1} data-index={index}
                tabIndex={isActive ? 0 : -1} className="virtual-cell">
                {renderCell(getItem(index), index, { active: isActive, tabIndex: -1 })}
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}
