import type { MediaFilters } from './types';

export function queryString(values: Record<string, unknown> | MediaFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length)) continue;
    params.set(key, Array.isArray(value) ? value.join(',') : String(value));
  }
  return params.toString();
}

async function responseError(response: Response): Promise<Error> {
  let detail = '';
  try {
    const body = await response.json();
    detail = typeof body.detail === 'string' ? body.detail : Array.isArray(body.detail)
      ? body.detail.map((item: { loc?: unknown[]; msg?: string }) => `${item.loc?.join('.') ?? 'Request'}: ${item.msg ?? 'Invalid value'}`).join('; ')
      : JSON.stringify(body.detail ?? body);
  } catch { /* Non-JSON proxy errors use the status description below. */ }
  return new Error(detail || `The local server returned ${response.status} ${response.statusText}. Check that the backend is running and try again.`);
}

export async function request<T>(path: string, options: { method?: string; body?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (method !== 'GET') headers['X-LFS-Request'] = '1';
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method, headers, signal: options.signal, credentials: 'same-origin',
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') throw error;
    throw new Error('Cannot reach the local server. Make sure Face Hunger is running, then retry.');
  }
  if (!response.ok) throw await responseError(response);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  try { return JSON.parse(text) as T; }
  catch { throw new Error('The local server returned an unexpected response. Check the backend and proxy configuration.'); }
}

export const mutate = <T = unknown>(path: string, body?: unknown, method = 'POST') => request<T>(path, { method, body });

export async function exportZip(path: string, body?: unknown, fallbackName = 'stillroom-export.zip'): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-LFS-Request': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    });
  } catch { throw new Error('Could not connect to create the export. Check the local server and retry.'); }
  if (!response.ok) throw await responseError(response);
  const blob = await response.blob();
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const name = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? fallbackName;
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name.replace(/[/\\]/g, '_');
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export async function batch<T>(items: T[], action: (item: T) => Promise<unknown>): Promise<void> {
  let cursor = 0;
  const failures: string[] = [];
  await Promise.all(Array.from({ length: Math.min(4, items.length) }, async () => {
    while (cursor < items.length) {
      const item = items[cursor++];
      try { await action(item); }
      catch (error) { failures.push(error instanceof Error ? error.message : String(error)); }
    }
  }));
  if (failures.length) throw new Error(`${items.length - failures.length} of ${items.length} completed. ${failures.length} failed: ${failures[0]} Refresh and retry only the remaining items.`);
}

export const number = (value: number) => new Intl.NumberFormat().format(value);
export const percent = (value: number | null) => value === null ? 'Not available' : `${Math.round(value * 100)}%`;
export function dateLabel(value: string | null): string {
  if (!value) return 'Date unknown';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}
export function timeLabel(value: number | null): string {
  if (value === null) return '--:--';
  const seconds = Math.max(0, Math.floor(value));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
export function bytes(value: number): string {
  if (!Number.isFinite(value)) return 'Unknown';
  if (value < 1024) return `${value} B`;
  const unit = Math.min(3, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** unit).toFixed(1)} ${['B', 'KB', 'MB', 'GB'][unit]}`;
}
