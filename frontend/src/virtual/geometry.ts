/** Pure layout math for virtualized grids (unit tested). */

export interface GridGeometry {
  columns: number;
  cellWidth: number;
  rowHeight: number;
  rows: number;
}

/** Fill the width with as many cells of at least `minCell` px as fit; rows are uniform. */
export function gridGeometry(width: number, count: number, minCell: number, gap: number, aspect = 1, extra = 0, minColumns = 1): GridGeometry {
  const usable = Math.max(minCell, width);
  const columns = Math.max(minColumns, 1, Math.floor((usable + gap) / (minCell + gap)));
  const cellWidth = (usable - gap * (columns - 1)) / columns;
  const rowHeight = Math.round(cellWidth * aspect + extra + gap);
  return { columns, cellWidth, rowHeight, rows: Math.ceil(count / columns) };
}

/** 1-based page numbers covering item indexes [start, end] (inclusive). */
export function pagesForRange(start: number, end: number, pageSize: number, total: number): number[] {
  if (total <= 0 || end < start) return [1];
  const first = Math.floor(Math.max(0, start) / pageSize) + 1;
  const last = Math.floor(Math.min(total - 1, end) / pageSize) + 1;
  const out: number[] = [];
  for (let page = first; page <= last; page++) out.push(page);
  return out;
}

/** Keyboard movement inside a grid of `count` items laid out in `columns`. */
export function moveIndex(index: number, key: string, columns: number, count: number, pageRows = 5): number | null {
  if (count <= 0) return null;
  const clamp = (value: number) => Math.max(0, Math.min(count - 1, value));
  switch (key) {
    case 'ArrowRight': return clamp(index + 1);
    case 'ArrowLeft': return clamp(index - 1);
    case 'ArrowDown': return clamp(index + columns);
    case 'ArrowUp': return clamp(index - columns);
    case 'PageDown': return clamp(index + columns * pageRows);
    case 'PageUp': return clamp(index - columns * pageRows);
    case 'Home': return 0;
    case 'End': return count - 1;
    default: return null;
  }
}

/** Items between two indexes (inclusive) that are loaded, for Shift-range selection. */
export function idsBetween<T extends { id: number }>(a: number, b: number, getItem: (index: number) => T | undefined): number[] {
  const [lo, hi] = a < b ? [a, b] : [b, a];
  const ids: number[] = [];
  for (let i = lo; i <= hi; i++) {
    const item = getItem(i);
    if (item) ids.push(item.id);
  }
  return ids;
}
