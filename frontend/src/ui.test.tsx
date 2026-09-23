import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { App } from './App';
import { AppProvider } from './context';
import { Empty, ErrorNotice, GallerySkeleton, PageHeader, Pagination, Thumbnail } from './components/ui';

const render = (node: React.ReactNode) => renderToStaticMarkup(<MemoryRouter>{node}</MemoryRouter>);

describe('shared UI semantics', () => {
  it('keeps page hierarchy, descriptions and actions accessible', () => {
    const html = render(<PageHeader eyebrow="Library" title="Your photographs" description="Your local collection" actions={<button>Import</button>} />);
    expect(html).toContain('<h1>Your photographs</h1>');
    expect(html).toContain('Your local collection');
    expect(html).toContain('<button>Import</button>');
  });
  it('provides one announced loading state with decorative skeletons', () => {
    const html = render(<GallerySkeleton label="Loading photographs" />);
    expect(html).toContain('role="status"');
    expect(html).toContain('aria-label="Loading photographs"');
    expect(html.match(/class="skeleton-tile"/g)).toHaveLength(12);
    expect(html.match(/aria-hidden="true"/g)).toHaveLength(12);
  });
  it('preserves empty-state recovery and labelled image fallbacks', () => {
    expect(render(<Empty title="No photographs" description="Connect a library"><button>Add library</button></Empty>)).toContain('<button>Add library</button>');
    expect(render(<Thumbnail src={null} alt="Holiday" />)).toContain('aria-label="Holiday: preview unavailable"');
    expect(render(<Thumbnail src={null} alt="" />)).not.toContain('role="img"');
  });
  it('announces errors and offers a retry control', () => {
    const html = render(<ErrorNotice error="Library disconnected" retry={() => {}} />);
    expect(html).toContain('role="alert"');
    expect(html).toContain('Try again');
  });
  it('disables unavailable pagination actions', () => {
    const html = render(<Pagination page={1} total={10} limit={60} onPage={() => {}} />);
    expect(html.match(/disabled=""/g)).toHaveLength(2);
    expect(html).toContain('aria-label="Pagination"');
  });
});

describe('application route rendering', () => {
  it.each(['/', '/photos', '/videos', '/people', '/clusters', '/review', '/duplicates', '/cleanup', '/search', '/settings', '/deleted', '/no-faces'])('renders %s without React errors', path => {
    const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      const html = renderToStaticMarkup(<MemoryRouter initialEntries={[path]}><AppProvider><App /></AppProvider></MemoryRouter>);
      expect(html).toContain('id="main-content"');
      expect(html).toContain('aria-label="Main navigation"');
      expect(html).toContain('aria-label="Settings"');
      expect(html).toMatch(/<h1>/);
      expect(errors).not.toHaveBeenCalled();
    } finally { errors.mockRestore(); }
  });
});
