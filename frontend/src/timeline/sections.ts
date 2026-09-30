/**
 * Timeline layout, pure and exact: every row's height is known from the day
 * counts alone, so the virtualizer never measures and scrolling never shifts.
 */

export type DayCount = [day: string, count: number];

export interface Section {
  key: string;
  /** Newest and oldest day in this section (ISO yyyy-mm-dd). */
  from: string;
  to: string;
  count: number;
  /** Index of the section's first item in the global newest-first media order. */
  start: number;
}

export type Row =
  | { kind: 'header'; section: number; top: number; height: number }
  | { kind: 'items'; section: number; first: number; count: number; top: number; height: number };

/**
 * "Intelligent" day grouping: busy days get their own section; runs of sparse
 * days within the same month are merged until a section has at least
 * `minItems` items, so a month of one-photo days is not 30 headers.
 */
export function groupDays(days: DayCount[], minItems = 8): Section[] {
  const sections: Section[] = [];
  let start = 0;
  let open: Section | null = null;
  for (const [day, count] of days) {
    const sameMonth = open !== null && open.to.slice(0, 7) === day.slice(0, 7);
    if (open && sameMonth && (open.count < minItems) && count < minItems) {
      open.to = day;
      open.count += count;
    } else {
      if (open) sections.push(open);
      open = { key: day, from: day, to: day, count, start };
    }
    start += count;
  }
  if (open) sections.push(open);
  return sections;
}

export function layoutRows(sections: Section[], columns: number, rowHeight: number, headerHeight: number): { rows: Row[]; height: number } {
  const rows: Row[] = [];
  let top = 0;
  sections.forEach((section, index) => {
    rows.push({ kind: 'header', section: index, top, height: headerHeight });
    top += headerHeight;
    for (let offset = 0; offset < section.count; offset += columns) {
      rows.push({ kind: 'items', section: index, first: section.start + offset, count: Math.min(columns, section.count - offset), top, height: rowHeight });
      top += rowHeight;
    }
  });
  return { rows, height: top };
}

/** Index of the last row whose top is <= y (binary search). */
export function rowAt(rows: Row[], y: number): number {
  let lo = 0, hi = rows.length - 1, found = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid].top <= y) { found = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return found;
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

export function sectionLabel(section: Pick<Section, 'from' | 'to'>, locale?: string): string {
  const fmt = (iso: string, opts: Intl.DateTimeFormatOptions) =>
    new Date(`${iso}T12:00:00`).toLocaleDateString(locale, opts);
  if (section.from === section.to) return fmt(section.from, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
  // Newest-first: `to` is the earlier day.
  const [a, b] = [section.to, section.from];
  if (a.slice(0, 7) === b.slice(0, 7)) {
    return `${fmt(a, { month: 'long', day: 'numeric' })} – ${Number(b.slice(8, 10))}, ${a.slice(0, 4)}`;
  }
  return `${fmt(a, { month: 'short', day: 'numeric', year: 'numeric' })} – ${fmt(b, { month: 'short', day: 'numeric', year: 'numeric' })}`;
}

export interface ScrubMark { label: string; year: string; month?: number; top: number }

/** Year and month anchors for the scrubber (first row of each). */
export function scrubMarks(sections: Section[], rows: Row[]): ScrubMark[] {
  const marks: ScrubMark[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    if (row.kind !== 'header') continue;
    const day = sections[row.section].from;
    const year = day.slice(0, 4);
    const month = Number(day.slice(5, 7));
    if (!seen.has(year)) { seen.add(year); marks.push({ label: year, year, top: row.top }); }
    const ym = `${year}-${month}`;
    if (!seen.has(ym)) { seen.add(ym); marks.push({ label: MONTHS[month - 1], year, month, top: row.top }); }
  }
  return marks;
}
