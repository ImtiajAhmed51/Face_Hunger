import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { invalidateResourceRequests, readResource } from './resourceRequests';
import { reconcileList } from './animatedList';
import type { AnimatedEntry } from './animatedList';
export type { AnimPhase, AnimatedEntry } from './animatedList';

export function refreshData(): void {
  invalidateResourceRequests();
  window.dispatchEvent(new Event('lfs:refresh'));
}

type ResourceState<T> = { path: string | null; data?: T; loading: boolean; refreshing: boolean; error: string };

export function useResource<T>(path: string | null, options?: { globalRefresh?: boolean }) {
  const globalRefresh = options?.globalRefresh !== false;
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<ResourceState<T>>({ path, loading: !!path, refreshing: false, error: '' });
  const reload = useCallback(() => setVersion(value => value + 1), []);
  useEffect(() => {
    if (!globalRefresh) return;
    window.addEventListener('lfs:refresh', reload);
    return () => window.removeEventListener('lfs:refresh', reload);
  }, [reload, globalRefresh]);
  useEffect(() => {
    if (!path) {
      setState({ path, loading: false, refreshing: false, error: '' });
      return;
    }
    const controller = new AbortController();
    setState(previous => {
      const data = previous.path === path ? previous.data : undefined;
      return { path, data, loading: data === undefined, refreshing: data !== undefined, error: '' };
    });
    const load = async () => {
      try {
        const data = await readResource<T>(path, controller.signal);
        if (!controller.signal.aborted) setState({ path, data, loading: false, refreshing: false, error: '' });
      } catch (error) {
        if (!controller.signal.aborted) setState(previous => ({ ...previous, path, loading: false, refreshing: false, error: error instanceof Error ? error.message : String(error) }));
      }
    };
    void load();
    return () => controller.abort();
  }, [path, version]);
  return {
    data: state.path === path ? state.data : undefined,
    loading: path !== null && (state.path !== path || state.loading),
    refreshing: state.path === path && state.refreshing,
    error: state.path === path ? state.error : '', reload,
  };
}

export function useDebounced<T>(value: T, delay = 250): T {
  const [result, setResult] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setResult(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return result;
}

export function useSelection(key: string) {
  const [state, setState] = useState<{ key: string; selected: Set<number> }>(() => ({ key, selected: new Set() }));
  const selected = state.key === key ? state.selected : new Set<number>();
  const update = useCallback((change: (current: Set<number>) => Set<number>) => {
    setState(current => ({ key, selected: change(current.key === key ? current.selected : new Set()) }));
  }, [key]);
  const toggle = useCallback((id: number) => update(current => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  }), [update]);
  const all = useCallback((ids: number[]) => update(current => {
    const next = new Set(current);
    const remove = ids.length > 0 && ids.every(id => next.has(id));
    for (const id of ids) { if (remove) next.delete(id); else next.add(id); }
    return next;
  }), [update]);
  const addRange = useCallback((ids: number[]) => update(current => {
    if (ids.every(id => current.has(id))) return current;
    return new Set([...current, ...ids]);
  }), [update]);
  const remove = useCallback((ids: number[]) => update(current => {
    const next = new Set(current);
    for (const id of ids) next.delete(id);
    return next;
  }), [update]);
  const clear = useCallback(() => update(() => new Set()), [update]);
  return { selected, toggle, all, addRange, remove, clear };
}

export function useMounted() {
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  return mounted;
}

export function useAnimatedList<T>(items: T[], getKey: (item: T) => string | number, exitMs = 340): AnimatedEntry<T>[] {
  const [rendered, setRendered] = useState<AnimatedEntry<T>[]>(() => items.map(item => ({ key: getKey(item), item, phase: 'shown' })));
  const previous = useRef(items);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useLayoutEffect(() => {
    if (previous.current.length === items.length && previous.current.every((item, index) => item === items[index])) return;
    previous.current = items;
    const animate = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    setRendered(current => reconcileList(current, items, getKey, animate));
  });
  useEffect(() => {
    if (!rendered.some(entry => entry.phase !== 'shown')) return;
    timer.current = setTimeout(() => {
      setRendered(current => current.filter(entry => entry.phase !== 'exit').map(entry => entry.phase === 'enter' ? { ...entry, phase: 'shown' } : entry));
    }, exitMs);
    return () => clearTimeout(timer.current);
  }, [rendered, exitMs]);
  return rendered;
}

export function useScrollLock() {
  const y = useRef(0);
  const frame = useRef(0);
  useEffect(() => () => cancelAnimationFrame(frame.current), []);
  const lock = useCallback(() => { y.current = window.scrollY; }, []);
  const restore = useCallback(() => {
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => window.scrollTo({ top: y.current, behavior: 'instant' }));
  }, []);
  return { lock, restore };
}

export function useFlip<T extends HTMLElement = HTMLDivElement>(deps: unknown, duration = 320) {
  const ref = useRef<T | null>(null);
  const prev = useRef<Map<string, DOMRect>>(new Map());
  useLayoutEffect(() => {
    const root = ref.current;
    if (!root) return;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const next = new Map<string, DOMRect>();
    const animations: Animation[] = [];
    // Measure all nodes before writing animation styles to avoid layout thrashing.
    const measurements = Array.from(root.querySelectorAll<HTMLElement>('[data-flip-id]'), node => ({ node, rect: node.getBoundingClientRect() }));
    for (const { node, rect } of measurements) {
      const id = node.dataset.flipId;
      if (!id) continue;
      next.set(id, rect);
      const first = prev.current.get(id);
      if (!first || reduced || !node.animate) continue;
      const dx = first.left - rect.left;
      const dy = first.top - rect.top;
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) continue;
      animations.push(node.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'translate(0, 0)' }], { duration, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' }));
    }
    prev.current = next;
    return () => animations.forEach(animation => animation.cancel());
  }, [deps, duration]);
  return ref;
}
