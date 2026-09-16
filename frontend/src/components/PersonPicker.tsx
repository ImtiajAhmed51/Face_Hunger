import { useId, useState } from 'react';
import { queryString } from '../api';
import { useDebounced, useResource } from '../hooks';
import type { Page, Person } from '../types';
import { ErrorNotice, Loading, Thumbnail } from './ui';
import { Icon } from './Icon';

export function PersonPicker({ onSelect, excluded = [], label = 'Find a person' }: { onSelect: (person: Person) => void; excluded?: number[]; label?: string }) {
  const [q, setQ] = useState('');
  const query = useDebounced(q);
  const resource = useResource<Page<Person>>(`/people?${queryString({ q: query, limit: 48 })}`);
  const id = useId();
  return <div className="person-picker"><label className="field" htmlFor={id}>{label}<div className="input-icon"><Icon name="search" size={18} /><input id={id} type="search" value={q} onChange={event => setQ(event.target.value)} placeholder="Search names..." autoComplete="off" /></div></label>
    <ErrorNotice error={resource.error} retry={resource.reload} />
    {resource.loading ? <Loading label="Finding people" /> : <div className="picker-results" role="group" aria-label="Matching people">
      {resource.data?.items.filter(person => !excluded.includes(person.id)).map(person => <button type="button" className="picker-person" key={person.id} onClick={() => onSelect(person)}>
        <Thumbnail src={person.representative_face_id ? `/api/faces/${person.representative_face_id}/thumbnail` : null} alt="" icon="people" /><span><strong>{person.display_name}</strong><small>{person.face_count} faces</small></span><Icon name="plus" size={16} />
      </button>)}
      {!resource.error && !resource.data?.items.some(person => !excluded.includes(person.id)) && <p className="muted">No people found. Try another name.</p>}
      {(resource.data?.total ?? 0) > 48 && <p className="muted small-text">Showing the first 48 matches. Type a name to narrow the list.</p>}
    </div>}
  </div>;
}
export function PersonToken({ id, onRemove }: { id: number; onRemove?: () => void }) {
  const { data, error } = useResource<Person>(`/people/${id}`);
  return <span className="person-token" title={error || undefined}>{data?.display_name ?? `Person #${id}`}{onRemove && <button type="button" onClick={onRemove} aria-label={`Remove ${data?.display_name ?? `person ${id}`} from filters`}><Icon name="close" size={14} /></button>}</span>;
}
