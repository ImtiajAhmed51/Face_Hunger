import { describe, expect, it } from 'vitest';
import { groupDays, layoutRows, rowAt, scrubMarks, sectionLabel, type DayCount } from './timeline/sections';
import { buildStyle, graticule, isLocalUrl, remoteUrls, bounds } from './map/style';
import { MESSAGES, translate, type MessageKey } from './i18n';

describe('timeline sections', () => {
  const days: DayCount[] = [
    ['2024-03-20', 40], ['2024-03-18', 2], ['2024-03-15', 1], ['2024-03-02', 3], ['2024-03-01', 4],
    ['2024-02-28', 1], ['2023-12-25', 12],
  ];
  it('keeps busy days alone and merges sparse days within a month', () => {
    const sections = groupDays(days, 8);
    expect(sections.map(s => [s.from, s.to, s.count, s.start])).toEqual([
      ['2024-03-20', '2024-03-20', 40, 0],
      ['2024-03-18', '2024-03-01', 10, 40],  // 2+1+3+4 merged until >= 8
      ['2024-02-28', '2024-02-28', 1, 50],   // new month starts a new section
      ['2023-12-25', '2023-12-25', 12, 51],
    ]);
    expect(sections.reduce((n, s) => n + s.count, 0)).toBe(63);
  });
  it('lays out exact rows so the virtual list never measures', () => {
    const sections = groupDays(days, 8);
    const { rows, height } = layoutRows(sections, 6, 100, 50);
    expect(rows.filter(r => r.kind === 'header')).toHaveLength(4);
    const itemRows = rows.filter(r => r.kind === 'items');
    expect(itemRows.reduce((n, r) => n + (r.kind === 'items' ? r.count : 0), 0)).toBe(63);
    expect(height).toBe(4 * 50 + itemRows.length * 100);
    // Every item index 0..62 appears exactly once, in order.
    const order = itemRows.flatMap(r => r.kind === 'items' ? Array.from({ length: r.count }, (_, i) => r.first + i) : []);
    expect(order).toEqual(Array.from({ length: 63 }, (_, i) => i));
    expect(rows[rowAt(rows, 0)].kind).toBe('header');
    expect(rowAt(rows, height + 1000)).toBe(rows.length - 1);
  });
  it('scales to 100k items without per-row measurement', () => {
    const many: DayCount[] = Array.from({ length: 3650 }, (_, i) => {
      const d = new Date(Date.UTC(2024, 0, 1) - i * 86_400_000).toISOString().slice(0, 10);
      return [d, 27 + (i % 7)];
    });
    const started = performance.now();
    const sections = groupDays(many);
    const { rows } = layoutRows(sections, 6, 160, 52);
    expect(performance.now() - started).toBeLessThan(100);
    expect(sections.reduce((n, s) => n + s.count, 0)).toBeGreaterThan(100_000);
    expect(rows.length).toBeGreaterThan(17_000);
    const marks = scrubMarks(sections, rows);
    expect(marks.filter(m => m.month === undefined).map(m => m.label).slice(0, 3)).toEqual(['2024', '2023', '2022']);
  });
  it('labels single days and ranges', () => {
    expect(sectionLabel({ from: '2024-03-18', to: '2024-03-01' }, 'en-US')).toBe('March 1 – 18, 2024');
    expect(sectionLabel({ from: '2024-03-20', to: '2024-03-20' }, 'en-US')).toContain('March 20, 2024');
  });
});

describe('local-only map style', () => {
  it('has no remote URLs, glyphs or sprites with tiles off', () => {
    const style = buildStyle();
    expect(remoteUrls(style)).toEqual([]);
    expect(style.glyphs).toBeUndefined();
    expect(style.sprite).toBeUndefined();
    expect(Object.keys(style.sources)).toEqual(['graticule']);
    expect(graticule().features.length).toBeGreaterThan(30);
  });
  it('reads a PMTiles basemap only from this origin', () => {
    const info = { tile_type: 'mvt', min_zoom: 0, max_zoom: 14, bounds: [-10, 40, 5, 55] as [number, number, number, number], vector_layers: ['earth', 'water', 'roads'] };
    const style = buildStyle({ pmtilesUrl: 'http://localhost:8765/api/map/tiles.pmtiles', pmtiles: info });
    expect(remoteUrls(style, 'http://localhost:8765')).toEqual([]);
    expect(style.layers.some(l => l.id === 'bm-water-fill')).toBe(true);
    const leaky = buildStyle({ pmtilesUrl: 'https://tiles.example.com/world.pmtiles', pmtiles: info });
    expect(remoteUrls(leaky, 'http://localhost:8765')).toEqual(['pmtiles://https://tiles.example.com/world.pmtiles']);
  });
  it('classifies URLs', () => {
    const origin = 'http://localhost:8765';
    expect(isLocalUrl('/api/map/points', origin)).toBe(true);
    expect(isLocalUrl('data:application/json,{}', origin)).toBe(true);
    expect(isLocalUrl('http://localhost:8765/api/x', origin)).toBe(true);
    expect(isLocalUrl('https://demotiles.maplibre.org/style.json', origin)).toBe(false);
    expect(isLocalUrl('//cdn.example.com/x', origin)).toBe(false);
    expect(bounds([[2, 48], [-74, 40], [139, 35]])).toEqual([-74, 35, 139, 48]);
  });
});

describe('i18n', () => {
  it('translates every key into Bangla and interpolates', () => {
    const missing = (Object.keys(MESSAGES.en) as MessageKey[]).filter(k => !MESSAGES.bn[k]);
    expect(missing).toEqual([]);
    expect(translate('timeline.count', { count: '1,234' }, 'en')).toBe('1,234 items');
    expect(translate('timeline.count', { count: '১২' }, 'bn')).toBe('১২টি আইটেম');
  });
});
