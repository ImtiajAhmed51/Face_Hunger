import { useState } from 'react';

type PagedItems<T> = {
  count: number;
  indexOf: (predicate: (item: T) => boolean) => number;
  getItemAt: (index: number) => Promise<T | undefined>;
};

/** Viewer navigation follows the complete result order, including uncached pages. */
export function usePagedViewer<T extends { id: number }>(items: PagedItems<T>) {
  const [current, setCurrent] = useState<{ id: number; index: number } | null>(null);
  const open = (id: number) => {
    const index = items.indexOf(item => item.id === id);
    if (index >= 0) setCurrent({ id, index });
  };
  const viewer = current ? {
    id: current.id,
    onClose: () => setCurrent(null),
    paging: {
      index: current.index,
      total: items.count,
      onStep: async (delta: number) => {
        const index = current.index + delta;
        const item = await items.getItemAt(index);
        if (item) setCurrent(previous => previous?.id === current.id ? { id: item.id, index } : previous);
      },
    },
  } : null;
  return { open, viewer };
}
