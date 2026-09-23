import { useEffect, useState } from 'react';
import { request } from './api';
import type { Media } from './types';

type Status = { status: string; progress: number; stage?: string; indeterminate?: boolean };
const activeStatuses = new Set(['analyzing', 'converting', 'verifying', 'replacing', 'pending']);

export function useConversionStatuses(items: Media[]) {
  const ids = items.filter(item => item.kind === 'video').map(item => item.id).sort((a, b) => a - b).join(',');
  const [state, setState] = useState<{ ids: string; statuses: Record<number, Status> }>({ ids: '', statuses: {} });
  useEffect(() => {
    if (!ids) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let running = false;
    const poll = async () => {
      if (running || controller.signal.aborted || document.hidden) return;
      running = true;
      let delay = 5000;
      try {
        const requested = ids.split(',');
        const statuses: Record<number, Status> = {};
        for (let offset = 0; offset < requested.length; offset += 120) {
          const chunk = requested.slice(offset, offset + 120);
          const data = await request<{ items: Record<string, Status> }>(`/media/conversion-statuses?ids=${encodeURIComponent(chunk.join(','))}`, { signal: controller.signal });
          for (const id of chunk) if (data.items[id]) statuses[Number(id)] = data.items[id];
        }
        if (controller.signal.aborted) return;
        setState(previous => previous.ids === ids && JSON.stringify(previous.statuses) === JSON.stringify(statuses) ? previous : { ids, statuses });
        delay = Object.values(statuses).some(status => activeStatuses.has(status.status)) ? 1500 : 10000;
      } catch { /* Keep the last known badges and retry; never stop after an HTTP error. */ }
      finally {
        running = false;
        if (!controller.signal.aborted && !document.hidden) timer = setTimeout(() => void poll(), delay);
      }
    };
    const visibility = () => { clearTimeout(timer); if (!document.hidden) void poll(); };
    document.addEventListener('visibilitychange', visibility);
    void poll();
    return () => { controller.abort(); clearTimeout(timer); document.removeEventListener('visibilitychange', visibility); };
  }, [ids]);
  return state.ids === ids ? state.statuses : {};
}
