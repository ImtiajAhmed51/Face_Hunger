import { useEffect, useId, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { number } from '../api';
import { useAction } from '../context';
import { Icon } from './Icon';
import type { IconName } from './Icon';

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description?: string; actions?: ReactNode }) {
  return <header className="page-header"><div>{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h1>{title}</h1>{description && <p className="page-description">{description}</p>}</div>{actions && <div className="header-actions">{actions}</div>}</header>;
}
export function Empty({ icon = 'photo', title, description, children }: { icon?: IconName; title: string; description: string; children?: ReactNode }) {
  return <div className="empty-state"><div className="empty-symbol"><Icon name={icon} size={34} /></div><h2>{title}</h2><p>{description}</p>{children && <div className="empty-actions">{children}</div>}</div>;
}
export function Loading({ label = 'Loading your library' }: { label?: string }) {
  return <div className="loading-state" role="status"><span className="spinner" />{label}<span className="sr-only">. Please wait.</span></div>;
}
export function ErrorNotice({ error, retry }: { error?: string; retry?: () => void }) {
  if (!error) return null;
  return <div className="error-notice" role="alert"><Icon name="alert" /><div><strong>Something needs attention</strong><p>{error}</p></div>{retry && <button className="button small" onClick={retry}>Try again</button>}</div>;
}
export function Badge({ children, tone = '' }: { children: ReactNode; tone?: 'teal' | 'amber' | 'red' | '' }) {
  return <span className={`badge ${tone}`}>{children}</span>;
}
export function Thumbnail({ src, alt, className = '', icon = 'photo' }: { src: string | null; alt: string; className?: string; icon?: IconName }) {
  const [state, setState] = useState<{ src: string | null; status: 'loaded' | 'failed' }>({ src: null, status: 'loaded' });
  const status = state.src === src ? state.status : 'loading';
  return <span className={`thumbnail ${status === 'loading' ? 'thumbnail-loading' : ''} ${className}`}>
    {src && status !== 'failed' ? <img key={src} src={src} alt={alt} loading="lazy" decoding="async"
      onLoad={() => setState({ src, status: 'loaded' })} onError={() => setState({ src, status: 'failed' })} />
      : <span className="thumbnail-fallback" role={alt ? 'img' : undefined} aria-hidden={!alt || undefined} aria-label={alt ? `${alt}: preview unavailable` : undefined}><Icon name={icon} size={30} /></span>}
  </span>;
}

export function GallerySkeleton({ label = 'Loading media' }: { label?: string }) {
  return <div role="status" aria-label={label} className="gallery-skeleton">
    <span className="sr-only">{label}</span>
    {Array.from({ length: 12 }, (_, index) => <div key={index} className="skeleton-tile" aria-hidden="true" />)}
  </div>;
}
export function Pagination({ page, limit, total, onPage }: { page: number; limit: number; total: number; onPage: (page: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / limit));
  useEffect(() => { if (page > pages) onPage(pages); }, [page, pages, onPage]);
  if (!total) return null;
  return <nav className="pagination" aria-label="Pagination"><span>{number(Math.min((page - 1) * limit + 1, total))}-{number(Math.min(page * limit, total))} of {number(total)}</span><div>
    <button className="button small" disabled={page <= 1} onClick={() => onPage(page - 1)} aria-label="Previous page"><Icon name="back" size={16} />Previous</button>
    <span className="page-number">{page} / {pages}</span>
    <button className="button small" disabled={page >= pages} onClick={() => onPage(page + 1)} aria-label="Next page">Next<Icon name="arrow" size={16} /></button>
  </div></nav>;
}
let openDialogs = 0;
let bodyOverflow = '';

export function Dialog({ open, onClose, title, children, className = '', busy = false }: { open: boolean; onClose: () => void; title: string; children: ReactNode; className?: string; busy?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog || !open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog.open) dialog.showModal();
    if (openDialogs++ === 0) { bodyOverflow = document.body.style.overflow; document.body.style.overflow = 'hidden'; }
    return () => {
      dialog.close();
      if (--openDialogs === 0) document.body.style.overflow = bodyOverflow;
      if (previous?.isConnected && (!openDialogs || previous.closest('dialog[open]'))) previous.focus({ preventScroll: true });
    };
  }, [open]);
  if (!open) return null;
  return <dialog ref={ref} className={`dialog ${className}`} aria-labelledby={titleId} aria-busy={busy}
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}
    onClick={event => {
      if (event.target !== ref.current || busy) return;
      const rect = ref.current.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
    }}>
    <div className="dialog-header"><h2 id={titleId}>{title}</h2><button className="icon-button" onClick={onClose} disabled={busy} aria-label="Close dialog"><Icon name="close" /></button></div>
    {children}
  </dialog>;
}
export function ConfirmDialog({ open, onClose, title, description, label = 'Confirm', confirmation, onConfirm, success, danger = true, refresh = true }: {
  open: boolean; onClose: () => void; title: string; description: string; label?: string; confirmation?: string;
  onConfirm: () => Promise<unknown>; success?: string; danger?: boolean; refresh?: boolean;
}) {
  const [value, setValue] = useState('');
  const action = useAction();
  useEffect(() => setValue(''), [open]);
  return <Dialog open={open} onClose={onClose} title={title} busy={action.busy}>
    <form onSubmit={async event => { event.preventDefault(); if (confirmation && value !== confirmation) return; if (await action.run(onConfirm, success, refresh)) onClose(); }}>
      <div className="dialog-body"><p>{description}</p>{confirmation && <label className="field">Type <strong>{confirmation}</strong> to continue<input autoFocus value={value} onChange={event => setValue(event.target.value)} autoComplete="off" spellCheck={false} required /></label>}<ErrorNotice error={action.error} /></div>
      <div className="dialog-footer"><button type="button" className="button" disabled={action.busy} onClick={onClose}>Cancel</button><button className={`button ${danger ? 'danger' : 'primary'}`} disabled={action.busy || (!!confirmation && value !== confirmation)}>{action.busy ? 'Working...' : label}</button></div>
    </form>
  </Dialog>;
}
export function SectionHeading({ title, meta, to, link = 'View all' }: { title: string; meta?: string; to?: string; link?: string }) {
  return <div className="section-heading"><div><h2>{title}</h2>{meta && <p>{meta}</p>}</div>{to && <Link className="text-link" to={to}>{link}<Icon name="arrow" size={16} /></Link>}</div>;
}
