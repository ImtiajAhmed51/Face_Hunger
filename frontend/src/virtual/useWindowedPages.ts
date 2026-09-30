import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQueries } from '@tanstack/react-query';
import { apiKey, fetchApi } from '../queryClient';
import type { Page } from '../types';
import { pagesForRange } from './geometry';

/**
 * Sparse, on-demand paging for virtualized lists: only pages overlapping the
 * visible range (plus one ahead) are fetched; everything else is a placeholder.
 * Pages are ordinary TanStack Query entries, so they are cached, deduplicated
 * and refreshed by `refreshData()` like any other resource.
 */
export type PageFetcher<T> = (page: number, limit: number, signal: AbortSignal) => Promise<Page<T>>;

export function useWindowedPages<T>(basePath: string | null, pageSize = 120, fetchPage?: PageFetcher<T>) {
  const [range, setRange] = useState<[number, number]>([0, pageSize - 1]);
  const [total, setTotal] = useState<{ path: string | null; value: number | null }>({ path: basePath, value: null });
  const knownTotal = total.path === basePath ? total.value : null;
  const pages = useMemo(() => {
    if (!basePath) return [];
    const wanted = pagesForRange(range[0], range[1] + pageSize, pageSize, knownTotal ?? pageSize);
    return wanted.includes(1) ? wanted : [1, ...wanted];
  }, [basePath, range, pageSize, knownTotal]);
  const separator = basePath?.includes('?') ? '&' : '?';
  const results = useQueries({
    queries: pages.map(page => {
      const path = `${basePath}${separator}page=${page}&limit=${pageSize}`;
      return {
        queryKey: fetchPage ? ['page', basePath, page, pageSize] : apiKey(path),
        queryFn: ({ signal }: { signal: AbortSignal }) => fetchPage ? fetchPage(page, pageSize, signal) : fetchApi<Page<T>>(path, signal),
        enabled: !!basePath,
      };
    }),
  });
  const byPage = new Map<number, T[]>();
  let latestTotal: number | null = null;
  results.forEach((result, i) => {
    if (result.data) {
      byPage.set(pages[i], result.data.items);
      latestTotal = result.data.total;
    }
  });
  // Remember the total so the list length stays stable while other pages load.
  useEffect(() => {
    if (latestTotal !== null && latestTotal !== knownTotal) setTotal({ path: basePath, value: latestTotal });
  }, [latestTotal, knownTotal, basePath]);
  const count = latestTotal ?? knownTotal ?? 0;
  const getItem = useCallback((index: number): T | undefined => {
    const page = Math.floor(index / pageSize) + 1;
    return byPage.get(page)?.[index % pageSize];
  }, [results, pageSize]); // byPage is derived from `results`
  const loaded = useMemo(() => pages.flatMap(page => byPage.get(page) ?? []), [results, pages]);
  const onRange = useCallback((start: number, end: number) => {
    setRange(current => {
      const a = Math.floor(start / pageSize), b = Math.floor(end / pageSize);
      return Math.floor(current[0] / pageSize) === a && Math.floor(current[1] / pageSize) === b ? current : [start, end];
    });
  }, [pageSize]);
  const first = results[0];
  const firstData = first?.data;
  const errored = results.find(result => result.error);
  return {
    /** The first page's full response (e.g. hybrid-search signals and warnings). */
    firstPage: firstData,
    count,
    getItem,
    loaded,
    onRange,
    loading: !!basePath && !!first && first.isPending,
    refreshing: results.some(result => result.isFetching && !result.isPending),
    error: errored?.error ? (errored.error instanceof Error ? errored.error.message : String(errored.error)) : '',
    reload: () => results.forEach(result => void result.refetch()),
  };
}
