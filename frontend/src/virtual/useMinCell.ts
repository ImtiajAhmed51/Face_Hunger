import { useSyncExternalStore } from 'react';

const PHONE = '(max-width: 720px)';
const TABLET = '(max-width: 1099px)';

function subscribe(callback: () => void) {
  const queries = [window.matchMedia(PHONE), window.matchMedia(TABLET)];
  queries.forEach((q) => q.addEventListener('change', callback));
  return () => queries.forEach((q) => q.removeEventListener('change', callback));
}

/** Smallest grid cell width for the current screen: [laptop, tablet, phone]. Re-evaluates when the breakpoint changes. */
export function useMinCell(laptop: number, tablet: number, phone: number): number {
  const size = useSyncExternalStore(
    subscribe,
    () => (window.matchMedia(PHONE).matches ? 2 : window.matchMedia(TABLET).matches ? 1 : 0),
    () => 0,
  );
  return [laptop, tablet, phone][size];
}
