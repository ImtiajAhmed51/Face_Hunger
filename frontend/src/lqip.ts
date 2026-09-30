import { useQuery } from '@tanstack/react-query';
import { request } from './api';

/** Blur-up placeholders for a set of media ids (one request per visible window, cached). */
export function useLqip(ids: number[]): Record<number, string> {
  const key = ids.slice(0, 240).join(',');
  const query = useQuery({
    queryKey: ['lqip', key],
    queryFn: ({ signal }) => request<{ items: Record<string, string> }>(`/media/lqip?ids=${key}`, { signal }),
    enabled: key.length > 0,
    staleTime: 10 * 60_000,
    meta: { noGlobalRefresh: true },
  });
  return (query.data?.items ?? {}) as unknown as Record<number, string>;
}
