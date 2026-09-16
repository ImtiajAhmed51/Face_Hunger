import { useEffect, useRef, useState } from 'react';
import { request } from './api';

export function refreshData(): void { window.dispatchEvent(new Event('lfs:refresh')); }

export function useResource<T>(path: string | null) {
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<{ path: string | null; data?: T; loading: boolean; error: string }>({ path, loading: !!path, error: '' });
  const reload = () => setVersion(value => value + 1);
  useEffect(() => {
    window.addEventListener('lfs:refresh', reload);
    return () => window.removeEventListener('lfs:refresh', reload);
  }, []);
  useEffect(() => {
    if (!path) { setState({ path, loading: false, error: '' }); return; }
    const controller = new AbortController();
    setState(previous => ({ path, data: previous.path === path ? previous.data : undefined, loading: true, error: '' }));
    void request<T>(path, { signal: controller.signal }).then(data => {
      if (!controller.signal.aborted) setState({ path, data, loading: false, error: '' });
    }).catch((error: Error) => {
      if (!controller.signal.aborted) setState(previous => ({ ...previous, path, loading: false, error: error.message }));
    });
    return () => controller.abort();
  }, [path, version]);
  return { data: state.path === path ? state.data : undefined, loading: state.path !== path || state.loading, error: state.path === path ? state.error : '', reload };
}

export function useDebounced<T>(value: T, delay = 250): T {
  const [result, setResult] = useState(value);
  useEffect(() => { const timer = window.setTimeout(() => setResult(value), delay); return () => window.clearTimeout(timer); }, [value, delay]);
  return result;
}

export function useSelection(key: string) {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  // Reset only when the filter/query key changes — not when paging within the same list.
  useEffect(() => setSelected(new Set()), [key]);
  const toggle = (id: number) => setSelected(current => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  // Toggle membership of the given ids only; leave other pages' selections intact.
  const all = (ids: number[]) => setSelected(current => {
    const next = new Set(current);
    const everySelected = ids.length > 0 && ids.every(id => next.has(id));
    if (everySelected) {
      for (const id of ids) next.delete(id);
    } else {
      for (const id of ids) next.add(id);
    }
    return next;
  });
  /** Add ids without removing existing selection (Shift+range / paint). */
  const addRange = (ids: number[]) => setSelected(current => {
    const next = new Set(current);
    for (const id of ids) next.add(id);
    return next;
  });
  return { selected, toggle, all, addRange, clear: () => setSelected(new Set()) };
}

export function useMounted() {
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  return mounted;
}
