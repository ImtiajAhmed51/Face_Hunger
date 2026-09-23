import { afterEach, describe, expect, it, vi } from 'vitest';
import { reconcileList } from './animatedList';
import { collectionFilters } from './mediaFilters';
import { invalidateResourceRequests, readResource } from './resourceRequests';
import { batch } from './api';

afterEach(() => { invalidateResourceRequests(); vi.unstubAllGlobals(); });

describe('animated list reconciliation', () => {
  it('updates metadata even when IDs do not change, and preserves server order', () => {
    const a = { id: 1, name: 'old' }; const b = { id: 2, name: 'two' };
    const previous = [a, b].map(item => ({ key: item.id, item, phase: 'shown' as const }));
    const updated = { ...a, name: 'new' };
    const next = reconcileList(previous, [b, updated], item => item.id);
    expect(next.map(entry => entry.key)).toEqual([2, 1]);
    expect(next[1].item.name).toBe('new');
  });
  it('revives a removed item and bounds retained exits', () => {
    const previous = Array.from({ length: 100 }, (_, id) => ({ key: id, item: { id }, phase: 'exit' as const }));
    const next = reconcileList(previous, [{ id: 50 }], item => item.id);
    expect(next[0].phase).toBe('shown');
    expect(next.length).toBe(17);
    expect(reconcileList(previous, [], item => item.id, false)).toEqual([]);
  });
});

it('keeps all structured search filters when collection controls are hidden', () => {
  const filters = { excluded: true, deleted: true, q: 'holiday', sort: 'name' as const };
  expect(collectionFilters(filters, { tools: false, deletedOnly: false, search: '', excluded: false, deleted: false, sort: 'date', excludePeople: [] })).toEqual(filters);
});

it('shares concurrent reads without cancelling other subscribers', async () => {
  let resolve!: (response: Response) => void;
  const fetchMock = vi.fn(() => new Promise<Response>(done => { resolve = done; }));
  vi.stubGlobal('fetch', fetchMock);
  const first = new AbortController(); const second = new AbortController();
  const a = readResource('/dashboard', first.signal);
  const b = readResource<{ count: number }>('/dashboard', second.signal);
  const rejected = expect(a).rejects.toMatchObject({ name: 'AbortError' });
  first.abort();
  resolve(new Response(JSON.stringify({ count: 2 })));
  await rejected;
  expect(await b).toEqual({ count: 2 });
  expect(fetchMock).toHaveBeenCalledTimes(1);
});

it('does not reuse pre-mutation reads after invalidation', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}')));
  const a = readResource('/media', new AbortController().signal);
  invalidateResourceRequests();
  const b = readResource('/media', new AbortController().signal);
  await Promise.all([a, b]);
  expect(fetch).toHaveBeenCalledTimes(2);
});

it('bounds batch concurrency and attempts every item after a partial failure', async () => {
  let active = 0; let peak = 0; const seen: number[] = [];
  await expect(batch([0, 1, 2, 3, 4, 5], async id => {
    active++; peak = Math.max(peak, active); seen.push(id);
    await Promise.resolve(); active--;
    if (id === 1) throw new Error('failed item');
  })).rejects.toThrow('5 of 6 completed');
  expect(peak).toBeLessThanOrEqual(4);
  expect(seen).toHaveLength(6);
});
