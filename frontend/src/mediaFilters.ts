import type { MediaFilters } from './types';

export function collectionFilters(filters: MediaFilters, controls: { tools: boolean; deletedOnly: boolean; search: string; excluded: boolean; deleted: boolean; sort: MediaFilters['sort']; excludePeople: number[] }): MediaFilters {
  if (!controls.tools) return { ...filters, ...(controls.deletedOnly ? { deleted: true } : {}) };
  return { ...filters, sort: controls.sort, q: controls.search, excluded: controls.deletedOnly ? false : controls.excluded, deleted: controls.deletedOnly || controls.deleted, ...(controls.excludePeople.length ? { exclude_people: controls.excludePeople } : {}) };
}
