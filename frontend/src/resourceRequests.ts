import { request } from './api';

type Pending = { controller: AbortController; promise: Promise<unknown>; users: number };
const pending = new Map<string, Pending>();

/** Invalidate sharing after a mutation without interrupting existing readers. */
export function invalidateResourceRequests(): void {
  pending.clear();
}

/** Share only in-flight GETs; each subscriber retains independent cancellation. */
export function readResource<T>(path: string, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'));
  let entry = pending.get(path);
  if (!entry) {
    const controller = new AbortController();
    const created: Pending = { controller, promise: request(path, { signal: controller.signal }), users: 0 };
    entry = created;
    pending.set(path, created);
    const forget = () => { if (pending.get(path) === created) pending.delete(path); };
    void created.promise.then(forget, forget);
  }
  const shared = entry;
  shared.users++;
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const release = () => {
      if (settled) return false;
      settled = true;
      signal.removeEventListener('abort', abort);
      shared.users--;
      // StrictMode can resubscribe before this microtask without issuing another GET.
      queueMicrotask(() => {
        if (shared.users === 0) {
          if (pending.get(path) === shared) pending.delete(path);
          shared.controller.abort();
        }
      });
      return true;
    };
    const abort = () => { if (release()) reject(new DOMException('Aborted', 'AbortError')); };
    signal.addEventListener('abort', abort, { once: true });
    void shared.promise.then(
      value => { if (release()) resolve(value as T); },
      error => { if (release()) reject(error); },
    );
  });
}
