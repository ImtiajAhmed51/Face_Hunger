import { mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import type { Page } from '@playwright/test';

export const OUT = resolve(process.cwd(), '../artifacts/visual');
export const VIEWPORTS: Record<string, [number, number]> = {
  '360x740': [360, 740], '390x844': [390, 844], '768x1024': [768, 1024], '1024x768': [1024, 768],
  '1280x800': [1280, 800], '1440x900': [1440, 900], '1920x1080': [1920, 1080], '2560x1440': [2560, 1440],
};
export type Theme = 'light' | 'dark' | 'hc';
export const EMPTY = 'http://127.0.0.1:8782';

export const SCREENS: Record<string, string> = {
  home: '/', people: '/people', person: '/people/3', clusters: '/clusters', photos: '/photos', videos: '/videos',
  'no-faces': '/no-faces', deleted: '/deleted', search: '/search', review: '/review', duplicates: '/duplicates',
  cleanup: '/cleanup', settings: '/settings', health: '/health', storage: '/storage', plugins: '/plugins',
  diagnostics: '/diagnostics', timeline: '/timeline', events: '/events', event: '/events/1', albums: '/albums',
  album: '/albums/2', favorites: '/favorites', map: '/map',
};

/** Theme, language and motion are set before the app boots, the way a returning user would have them. */
export async function prepare(page: Page, theme: Theme, lang: 'en' | 'bn', problems: string[]) {
  await page.emulateMedia({ colorScheme: theme === 'dark' ? 'dark' : 'light', reducedMotion: 'reduce',
    forcedColors: theme === 'hc' ? 'active' : 'none', contrast: theme === 'hc' ? 'more' : 'no-preference' });
  await page.addInitScript(([t, l]) => {
    try { localStorage.setItem('lfs-theme', t === 'hc' ? 'system' : t); localStorage.setItem('lfs-lang', l); } catch { /* ignore */ }
  }, [theme, lang]);
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`console: ${m.text().slice(0, 200)}`); });
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message.slice(0, 200)}`));
}

export async function settle(page: Page) {
  await page.waitForTimeout(250);
  // Wait for what is on screen: skeletons gone and visible images decoded (offscreen lazy images never load).
  await page.waitForFunction(() => {
    const visible = (el: Element) => { const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight && r.width > 0; };
    if ([...document.querySelectorAll('.gallery-skeleton, .skeleton-tile, .cell-skeleton, .loading-state')].some(visible)) return false;
    return [...document.images].filter(visible).every((i) => i.complete);
  }, null, { timeout: 5000 }).catch(() => {});
  await page.evaluate(() => document.fonts.ready);
  await page.addStyleTag({ content: '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}' });
  await page.waitForTimeout(200);
}

/** Layout facts measured in the page: broken images, horizontal overflow, clipped text, tiny text. */
export async function measure(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out: string[] = [];
    const doc = document.documentElement;
    if (doc.scrollWidth > doc.clientWidth + 1) out.push(`horizontal overflow ${doc.scrollWidth - doc.clientWidth}px`);
    for (const img of document.images) if (img.complete && img.naturalWidth === 0 && img.offsetParent) out.push(`broken image ${img.src.split('/').slice(-3).join('/')}`);
    const seen = new Set<string>();
    for (const el of document.querySelectorAll<HTMLElement>('body *')) {
      if (!el.offsetParent || !el.childNodes.length) continue;
      const text = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent ?? '').join('').trim();
      if (!text) continue;
      const style = getComputedStyle(el);
      const size = parseFloat(style.fontSize);
      if (size < 11.9 && !el.closest('.sr-only')) { const k = `tiny text ${size}px .${el.className}`; if (!seen.has(k)) { seen.add(k); out.push(k); } }
      if (/^[a-z]+(\.[A-Za-z_]+){1,3}$/.test(text)) out.push(`raw i18n key? "${text}"`);
      if (style.overflow === 'visible' && el.scrollWidth > el.clientWidth + 2 && style.display !== 'inline' && el.getBoundingClientRect().width > 0) {
        const k = `text overflows its box: .${el.className} "${text.slice(0, 30)}"`; if (!seen.has(k)) { seen.add(k); out.push(k); }
      }
    }
    return out.slice(0, 25);
  });
}

export function shotPath(screen: string, state: string, viewport: string, theme: string, lang: string): string {
  const file = join(OUT, screen, `${state}-${viewport}-${theme}-${lang}.png`);
  mkdirSync(dirname(file), { recursive: true });
  return file;
}
