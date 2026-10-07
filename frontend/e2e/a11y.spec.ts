import AxeBuilder from '@axe-core/playwright';
import type { Page } from '@playwright/test';
import { expect, go, test } from './fixtures';

const TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];
async function audit(page: Page, label: string, found: string[]) {
  const results = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  for (const v of results.violations) found.push(`${label}: ${v.id} (${v.impact}) ${v.nodes.length}x ${v.nodes.slice(0, 3).map((n) => n.target.join(' ')).join(' | ')}`);
}

// WCAG 2.2 AA audit of every screen, in both colour schemes and at phone width.
const SCREENS = ['/', '/people', '/people/1', '/clusters', '/photos', '/videos', '/no-faces', '/deleted', '/search', '/review',
  '/duplicates', '/cleanup', '/settings', '/health', '/storage', '/plugins', '/diagnostics', '/timeline', '/events', '/events/1',
  '/albums', '/favorites', '/map'];

for (const scheme of ['light', 'dark'] as const) {
  test(`axe: all screens, ${scheme}`, async ({ page, errors }) => {
    void errors;
    const found: string[] = [];
    test.setTimeout(180_000);
    await page.emulateMedia({ colorScheme: scheme });
    for (const screen of SCREENS) {
      await go(page, screen);
      await expect(page.locator('h1').first()).toBeVisible();
      await audit(page, `${screen} (${scheme})`, found);
    }
    expect(found).toEqual([]);
  });
}

test('axe: core screens at phone width', async ({ page, errors }) => {
  void errors;
  const found: string[] = [];
  await page.setViewportSize({ width: 375, height: 812 });
  for (const screen of ['/', '/photos', '/people', '/search', '/timeline', '/storage', '/plugins', '/settings']) {
    await go(page, screen);
    await expect(page.locator('h1').first()).toBeVisible();
    await audit(page, `${screen} (phone)`, found);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (overflow > 1) found.push(`${screen} (phone): horizontal scroll ${overflow}px`);
  }
  expect(found).toEqual([]);
});

test('keyboard: every screen has one h1, a main landmark, and a visible focus ring', async ({ page, errors }) => {
  void errors;
  for (const screen of SCREENS) {
    await go(page, screen);
    await expect(page.locator('h1')).toHaveCount(1);
    await expect(page.locator('main')).toHaveCount(1);
    expect(await page.title()).not.toBe('');
    expect(await page.locator('html').getAttribute('lang')).toBeTruthy();
  }
  await go(page, '/photos');
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  const ring = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el || el === document.body) return null;
    const style = getComputedStyle(el);
    return { outline: style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) >= 2, shadow: style.boxShadow !== 'none' };
  });
  expect(ring && (ring.outline || ring.shadow)).toBeTruthy();
});
