import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { gridGeometry, idsBetween, moveIndex, pagesForRange } from './virtual/geometry';
import { VirtualGrid } from './virtual/VirtualGrid';
import { UndoStack } from './undo';
import { rankCommands, score, type Command } from './commands';
import { AppProvider } from './context';
import { CommandPalette } from './components/CommandPalette';

describe('virtual grid geometry', () => {
  it('fits columns to width and keeps rows uniform at 100k items', () => {
    const g = gridGeometry(1000, 100_000, 190, 14, 1, 74);
    expect(g.columns).toBe(4);
    expect(g.rows).toBe(25_000);
    expect(g.rowHeight).toBe(Math.round(g.cellWidth + 74 + 14));
    expect(g.rows * g.rowHeight).toBeLessThan(33_000_000); // below browser element-height limits
    expect(gridGeometry(100, 5, 190, 14).columns).toBe(1);
  });
  it('maps visible ranges to the pages to fetch', () => {
    expect(pagesForRange(0, 50, 120, 1000)).toEqual([1]);
    expect(pagesForRange(100, 250, 120, 1000)).toEqual([1, 2, 3]);
    expect(pagesForRange(990, 5000, 120, 1000)).toEqual([9]);
    expect(pagesForRange(0, 10, 120, 0)).toEqual([1]);
  });
  it('moves keyboard focus within bounds', () => {
    expect(moveIndex(5, 'ArrowDown', 4, 10)).toBe(9);
    expect(moveIndex(9, 'ArrowDown', 4, 10)).toBe(9);
    expect(moveIndex(0, 'ArrowLeft', 4, 10)).toBe(0);
    expect(moveIndex(3, 'End', 4, 10)).toBe(9);
    expect(moveIndex(9, 'PageUp', 4, 100, 2)).toBe(1);
    expect(moveIndex(1, 'x', 4, 10)).toBeNull();
  });
  it('selects only loaded items across a Shift range', () => {
    const loaded = (i: number) => (i === 2 ? undefined : { id: 100 + i });
    expect(idsBetween(4, 0, loaded)).toEqual([100, 101, 103, 104]);
  });
});

describe('undo stack', () => {
  it('undoes newest first, supports targeted undo and restores on failure', async () => {
    const stack = new UndoStack(3);
    const log: string[] = [];
    stack.push('a', () => log.push('a'));
    const b = stack.push('b', () => log.push('b'));
    stack.push('c', () => { throw new Error('offline'); });
    await expect(stack.undo()).rejects.toThrow('offline');
    expect(stack.size).toBe(3); // failed undo stays available
    expect(await stack.undo(b.id)).toBe('b');
    stack.push('d', () => log.push('d'));
    stack.push('e', () => log.push('e'));
    expect(stack.size).toBe(3); // bounded
    expect(await stack.undo()).toBe('e');
    expect(log).toEqual(['b', 'e']);
  });
  it('notifies subscribers', () => {
    const stack = new UndoStack();
    const listener = vi.fn();
    const off = stack.subscribe(listener);
    stack.push('x', () => {});
    off();
    stack.push('y', () => {});
    expect(listener).toHaveBeenCalledTimes(1);
  });
});

describe('command palette ranking', () => {
  const cmd = (label: string, group: Command['group'] = 'Go to', keywords = ''): Command => ({ id: label, label, group, keywords, run: () => {} });
  it('prefers prefix, then word, then subsequence matches', () => {
    expect(score('People', 'peo')).toBeGreaterThan(score('Deleted people', 'peo'));
    expect(score('No faces', 'nf')).toBeGreaterThan(0);
    expect(score('Settings', 'xyz')).toBe(0);
  });
  it('filters, keeps search fallback and orders groups when empty', () => {
    const list = [cmd('Search for “dogs”', 'Search'), cmd('Settings'), cmd('Review'), cmd('Theme: Dark', 'Actions', 'appearance')];
    expect(rankCommands(list, 'rev').map(c => c.label)).toEqual(['Review', 'Search for “dogs”']);
    expect(rankCommands(list, 'appear').map(c => c.label)[0]).toBe('Theme: Dark');
    expect(rankCommands(list, '').map(c => c.group)).toEqual(['Go to', 'Go to', 'Actions', 'Search']);
  });
});

describe('accessible markup', () => {
  it('renders the virtual grid as an ARIA grid with one tab stop and skeleton placeholders', () => {
    const html = renderToStaticMarkup(
      <VirtualGrid<{ id: number }> label="Photos" count={10} getItem={i => (i < 4 ? { id: i } : undefined)}
        renderCell={(item) => item ? <span>item {item.id}</span> : <div className="cell-skeleton" aria-hidden="true" />} />,
    );
    expect(html).toContain('role="grid"');
    expect(html).toContain('aria-label="Photos"');
    expect(html.match(/tabindex="0"/g)).toHaveLength(1);
    expect(html).toContain('role="gridcell"');
    expect(html).toContain('cell-skeleton');
  });
  it('renders the command palette as a labelled combobox + listbox', () => {
    const client = new QueryClient();
    const html = renderToStaticMarkup(
      <QueryClientProvider client={client}><MemoryRouter><AppProvider>
        <CommandPalette open onClose={() => {}} pages={[{ to: '/', label: 'Overview' }, { to: '/people', label: 'People' }]} />
      </AppProvider></MemoryRouter></QueryClientProvider>,
    );
    // <dialog> content is rendered by the Dialog component only when open.
    expect(html).toContain('role="combobox"');
    expect(html).toContain('role="listbox"');
    expect(html).toContain('aria-activedescendant="palette-go:/"');
    expect(html).toContain('role="option"');
  });
});
