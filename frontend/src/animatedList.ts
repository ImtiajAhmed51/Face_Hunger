export type AnimPhase = 'enter' | 'shown' | 'exit';
export type AnimatedEntry<T> = { key: string | number; item: T; phase: AnimPhase };

/** Server order and fresh item data take precedence over previous animation state. */
export function reconcileList<T>(previous: AnimatedEntry<T>[], items: T[], keyOf: (item: T) => string | number, animate = true): AnimatedEntry<T>[] {
  const previousByKey = new Map(previous.map(entry => [entry.key, entry]));
  const keys = new Set(items.map(keyOf));
  const next = items.map(item => {
    const key = keyOf(item);
    const old = previousByKey.get(key);
    const phase: AnimPhase = !animate || old?.phase === 'exit' ? 'shown' : old?.phase ?? 'enter';
    return old?.item === item && old.phase === phase ? old : { key, item, phase };
  });
  if (animate) {
    next.push(...previous.filter(entry => !keys.has(entry.key)).slice(0, 16).map(entry => ({ ...entry, phase: 'exit' as const })));
  }
  return next;
}
