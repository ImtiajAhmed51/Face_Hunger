import { QueryClient } from '@tanstack/react-query';
import { request } from './api';

/** One cache for the whole app. The server is local, so we favour freshness over caching. */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      gcTime: 5 * 60_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

export const apiKey = (path: string) => ['api', path] as const;

export function fetchApi<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}
