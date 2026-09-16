import { useState } from 'react';
import { mutate, number } from '../api';
import { useAction, useApp } from '../context';
import { useResource } from '../hooks';
import type { Library } from '../types';
import { Icon } from './Icon';
import { ConfirmDialog, Dialog, Empty, ErrorNotice, Loading } from './ui';

function LibraryCard({ library }: { library: Library }) {
  const [edit, setEdit] = useState(false);
  const [remove, setRemove] = useState(false);
  const [force, setForce] = useState(false);
  const [ignored, setIgnored] = useState(library.ignored.join('\n'));
  const action = useAction();
  const { job } = useApp();
  const active = !!job && ['running', 'queued', 'paused'].includes(job.status);
  return <article className="library-card"><div className="library-card-heading"><div className="folder-icon"><Icon name="folder" size={25} /></div><div><h3>{library.name}</h3><p className="library-path">{library.path}</p></div><span className="badge">{number(library.media_count)} files</span></div>
    {library.ignored.length > 0 && <p className="ignored-summary">Ignored: {library.ignored.join(', ')}</p>}
    <div className="library-actions"><button className="button primary small" disabled={action.busy || active} onClick={() => void action.run(() => mutate('/index', { library_id: library.id }), 'Library scan started.')}><Icon name="play" size={15} />Scan for changes</button><button className="button small" disabled={action.busy || active} onClick={() => void action.run(() => mutate('/index', { library_id: library.id, retry_failed: true }), 'Retry scan started.')}>Retry failed</button><button className="button small" disabled={action.busy || active} onClick={() => setForce(true)}>Rescan all</button><button className="button ghost small" disabled={action.busy} onClick={() => { setIgnored(library.ignored.join('\n')); setEdit(true); }}>Ignored folders</button><button className="icon-button danger-text" aria-label={`Remove ${library.name} from the index`} disabled={active || action.busy} onClick={() => setRemove(true)}><Icon name="trash" size={17} /></button></div>
    <Dialog open={edit} onClose={() => setEdit(false)} title={`Ignored folders in ${library.name}`} busy={action.busy}><form onSubmit={async event => { event.preventDefault(); if (await action.run(() => mutate(`/libraries/${library.id}`, { ignored: ignored.split('\n').map(value => value.trim()).filter(Boolean) }, 'PATCH'), 'Ignored folders updated.')) setEdit(false); }}><div className="dialog-body"><p>Enter one folder per line. These folders will be skipped during indexing. Changing this list does not delete originals.</p><label className="field">Ignored folders<textarea rows={7} autoFocus value={ignored} onChange={event => setIgnored(event.target.value)} placeholder="One folder path per line" /></label><ErrorNotice error={action.error} /></div><div className="dialog-footer"><button type="button" className="button" disabled={action.busy} onClick={() => setEdit(false)}>Cancel</button><button className="button primary" disabled={action.busy}>{action.busy ? 'Saving...' : 'Save ignored folders'}</button></div></form></Dialog>
    <ConfirmDialog open={remove} onClose={() => setRemove(false)} title={`Remove ${library.name}?`} description="All indexed media, faces, and corrections for this library will be removed from the local index. Original files remain untouched. Re-adding the library requires a new scan." label="Remove library" confirmation="REMOVE LIBRARY" onConfirm={() => mutate(`/libraries/${library.id}`, { confirm: 'REMOVE LIBRARY' }, 'DELETE')} success="Library removed from the index. Original files were not changed." />
    <ConfirmDialog open={force} onClose={() => setForce(false)} title={`Rescan all of ${library.name}?`} description="Force processing of the library, including unchanged files. This may take a while. Use Scan for changes for a faster incremental scan." label="Rescan all files" danger={false} onConfirm={() => mutate('/index', { library_id: library.id, force: true })} success="Full rescan started." />
  </article>;
}

export function Libraries({ roots }: { roots: string[] }) {
  const resource = useResource<{ items: Library[] }>('/libraries');
  const [add, setAdd] = useState(false);
  const [path, setPath] = useState('');
  const [ignored, setIgnored] = useState('');
  const action = useAction();
  return <section className="settings-section" id="libraries"><div className="section-heading"><div><p className="eyebrow">WHERE YOUR MEMORIES LIVE</p><h2>Libraries</h2></div><button className="button primary small" onClick={() => setAdd(true)}><Icon name="plus" size={16} />Add library</button></div><p className="muted">Connect folders on the machine running the local server. Nothing is uploaded or moved.</p><ErrorNotice error={resource.error} retry={resource.reload} />
    {resource.loading && !resource.data ? <Loading label="Loading libraries" /> : resource.data?.items.length ? <div className="libraries-list">{resource.data.items.map(library => <LibraryCard library={library} key={library.id} />)}</div> : resource.data && <Empty icon="folder" title="Your first library starts here" description="Choose a folder of photos or videos. Stillroom will index it in place, with your originals always untouched."><button className="button" onClick={() => setAdd(true)}><Icon name="plus" size={16} />Connect a folder</button></Empty>}
    <Dialog open={add} onClose={() => setAdd(false)} title="Connect a library" busy={action.busy}><form onSubmit={async event => { event.preventDefault(); if (await action.run(() => mutate('/libraries', { path: path.trim(), ignored: ignored.split('\n').map(value => value.trim()).filter(Boolean) }), 'Library added. Start a scan when you are ready.')) { setAdd(false); setPath(''); setIgnored(''); } }}><div className="dialog-body"><p>Enter the full path to an existing folder on the local server's machine. Browser file uploads are not needed.</p><label className="field">Library folder path<input autoFocus value={path} onChange={event => setPath(event.target.value)} placeholder="Full path to your photos or videos" required /></label><label className="field">Ignored folders <span className="muted">(optional, one per line)</span><textarea rows={3} value={ignored} onChange={event => setIgnored(event.target.value)} /></label>{roots.length > 0 && <div className="allowed-roots"><strong>Allowed roots</strong>{roots.map(root => <code key={root}>{root}</code>)}</div>}<ErrorNotice error={action.error} /></div><div className="dialog-footer"><button className="button" type="button" disabled={action.busy} onClick={() => setAdd(false)}>Cancel</button><button className="button primary" disabled={action.busy || !path.trim()}>{action.busy ? 'Connecting...' : 'Connect library'}</button></div></form></Dialog>
  </section>;
}
