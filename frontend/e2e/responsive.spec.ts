import { mkdirSync } from 'node:fs';
import type { Page } from '@playwright/test';
import { expect, go, test } from './fixtures';

const screens = ['/photos', '/search', '/people', '/albums', '/favorites', '/recently-added', '/videos',
  '/events', '/map', '/review', '/clusters', '/libraries', '/settings', '/overview', '/browse',
  '/no-faces', '/deleted', '/duplicates', '/cleanup', '/storage', '/health', '/plugins', '/diagnostics',
  '/timeline', '/people/1', '/events/1'];

async function checkLayout(page: Page, label: string, found: string[]) {
  await page.waitForFunction(() => document.getAnimations().every(a => a.playState !== 'running'), null,
    { timeout: 2000 }).catch(() => {});
  const layout = await page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const outside = [...document.querySelectorAll('main button, main input, main select, main h1, main h2, dialog[open] button, dialog[open] input')].flatMap(el => {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || getComputedStyle(el).visibility === 'hidden') return [];
      // Horizontally scrolling strips may legitimately contain offscreen items.
      for (let parent = el.parentElement; parent && parent !== document.body && parent.tagName !== 'MAIN'; parent = parent.parentElement) {
        if (parent instanceof HTMLDetailsElement && !parent.open && !el.closest('summary')) return [];
        if (['auto', 'scroll'].includes(getComputedStyle(parent).overflowX)) return [];
      }
      return r.left < -1 || r.right > width + 1 ? [`${el.tagName}.${el.className}: ${el.textContent?.trim().slice(0, 60)}`] : [];
    });
    const dialogs = [...document.querySelectorAll('dialog[open]')].flatMap(el => {
      const r = el.getBoundingClientRect();
      return r.left < -1 || r.right > width + 1 || r.top < -1 || r.bottom > innerHeight + 1 ? ['dialog outside viewport'] : [];
    });
    return { overflow: document.documentElement.scrollWidth - width, outside, dialogs };
  });
  if (layout.overflow > 1) found.push(`${label}: horizontal overflow ${layout.overflow}px`);
  found.push(...layout.outside.map(issue => `${label}: ${issue}`), ...layout.dialogs.map(issue => `${label}: ${issue}`));
  await page.screenshot({ path: `test-results/responsive/${label.replaceAll(/[^a-zA-Z0-9-]/g, '-')}.png` });
}

for (const [device, width, height] of [['desktop', 1440, 900], ['tablet', 820, 1180], ['mobile', 390, 844], ['small-mobile', 320, 740]] as const) {
  test(`responsive: every tab and nested views on ${device}`, async ({ page, errors }) => {
    void errors;
    test.setTimeout(240_000);
    mkdirSync('test-results/responsive', { recursive: true });
    await page.setViewportSize({ width, height });
    const found: string[] = [];
    const album = await page.request.post('/api/albums', { headers: { 'X-LFS-Request': '1' },
      data: { name: `Responsive ${device}`, media_ids: [1, 2] } }).then(r => r.json());
    const collection = await page.request.post('/api/collections', { headers: { 'X-LFS-Request': '1' },
      data: { name: `Responsive smart ${device}`, query: { kind: 'photo' } } }).then(r => r.json());
    const routes = [...screens, `/albums/${album.album_id}`, `/collections/${collection.id}`];
    for (const route of routes) {
      await go(page, route);
      await expect(page.locator('h1').first()).toBeVisible();
      await expect(page.getByRole('button', { name: 'Reload application' })).toHaveCount(0);
      await checkLayout(page, `${device}${route}`, found);
    }
    if (width > 720) {
      const sidebar = page.locator('.sidebar');
      const label = sidebar.getByRole('link', { name: 'Photos', exact: true }).locator('span');
      await expect(label).toBeVisible();
      await page.getByRole('button', { name: 'Collapse sidebar' }).click();
      await expect(label).toBeHidden();
      await expect.poll(async () => (await sidebar.boundingBox())!.width).toBe(76);
      await checkLayout(page, `${device}-collapsed-sidebar`, found);
      await page.getByRole('button', { name: 'Expand sidebar' }).click();
      await expect(label).toBeVisible();
      await expect.poll(async () => (await sidebar.boundingBox())!.width).toBe(232);
    }
    await page.route('**/api/dashboard', async route => {
      const response = await route.fetch();
      const dashboard = await response.json();
      await route.fulfill({ response, json: { ...dashboard, faces: 165160 } });
    });
    await go(page, '/overview');
    const count = page.locator('.stat-card').filter({ hasText: 'Faces found' }).locator('strong');
    await expect(count).toHaveText('165,160');
    expect(await count.evaluate(el => {
      const range = document.createRange();
      range.selectNodeContents(el);
      return range.getClientRects().length;
    }), 'large library count stays on one line').toBe(1);
    await checkLayout(page, `${device}-large-library-overview`, found);
    await page.unroute('**/api/dashboard');
    await go(page, '/cleanup');
    const categories = page.getByRole('group', { name: 'Cleanup category' }).getByRole('button');
    for (let i = 0; i < await categories.count(); i++) {
      await categories.nth(i).click();
      await checkLayout(page, `${device}-cleanup-${i}`, found);
    }
    await go(page, '/people/1');
    const profileViews = page.getByRole('group', { name: 'Profile view' }).getByRole('button');
    for (let i = 0; i < await profileViews.count(); i++) {
      await profileViews.nth(i).click();
      await checkLayout(page, `${device}-profile-view-${i}`, found);
    }
    await page.getByLabel('Select faces on page', { exact: true }).check();
    await checkLayout(page, `${device}-face-selection`, found);
    await page.getByRole('button', { name: 'Move / split', exact: true }).click();
    await expect(page.locator('dialog[open]')).toBeVisible();
    await checkLayout(page, `${device}-move-faces`, found);
    await page.keyboard.press('Escape');
    const moreActions = page.locator('.profile-actions .actions-menu');
    await moreActions.locator('summary').click();
    await checkLayout(page, `${device}-person-menu`, found);
    await moreActions.locator('summary').click();
    await expect(page.getByRole('button', { name: 'Encrypted export', exact: true })).toHaveCount(0);
    for (const name of ['Rename', 'Merge']) {
      await page.getByRole('button', { name, exact: true }).click();
      await expect(page.locator('dialog[open]')).toBeVisible();
      await checkLayout(page, `${device}-person-${name}`, found);
      await page.keyboard.press('Escape');
      await expect(page.locator('dialog[open]')).toHaveCount(0);
    }
    await go(page, '/photos');
    await page.locator('.timeline-tile').first().click();
    await expect(page.locator('.viewer-dialog')).toBeVisible();
    await checkLayout(page, `${device}-photo-viewer`, found);
    await page.keyboard.press('Escape');
    await go(page, '/search');
    await page.getByRole('button', { name: 'Build a precise search', exact: true }).click();
    await checkLayout(page, `${device}-search-filters`, found);
    await page.getByRole('button', { name: 'Add person', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Who are you looking for?' })).toBeVisible();
    await checkLayout(page, `${device}-search-people`, found);
    await page.keyboard.press('Escape');
    await go(page, '/review');
    const decisionButtons = page.locator('.review-primary-actions .button');
    await expect(decisionButtons).toHaveCount(2);
    if (width <= 720) {
      for (const button of await decisionButtons.all()) {
        const bounds = await button.boundingBox();
        expect(bounds?.height).toBeGreaterThanOrEqual(44);
        expect(bounds?.height).toBeLessThanOrEqual(52);
      }
    }
    await page.getByRole('button', { name: 'Filter by person', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Review one person' })).toBeVisible();
    await checkLayout(page, `${device}-review-people`, found);
    await page.keyboard.press('Escape');
    await go(page, '/videos');
    await page.locator('main').getByRole('button', { name: /^Open / }).first().click();
    await expect(page.locator('.viewer-dialog video')).toBeVisible();
    await checkLayout(page, `${device}-video-viewer`, found);
    await page.keyboard.press('Escape');
    await go(page, '/libraries');
    await page.getByRole('button', { name: 'Add library', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await checkLayout(page, `${device}-folder-chooser`, found);
    await page.getByRole('button', { name: 'Connect to network share' }).click();
    await expect(page.getByRole('dialog', { name: 'Connect to a network share' })).toBeVisible();
    await checkLayout(page, `${device}-network-share`, found);
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    if (width <= 720) {
      await page.getByRole('button', { name: 'More pages' }).click();
      await expect(page.getByRole('dialog', { name: 'Your collection' })).toBeVisible();
      const menu = page.locator('.mobile-navigation');
      await expect.poll(async () => (await menu.boundingBox())?.x).toBeCloseTo(0, 0);
      await expect.poll(async () => (await menu.boundingBox())?.width).toBeCloseTo(width, 0);
      await menu.locator('summary').click();
      const toolLinks = menu.locator('.nav-tools-links .nav-link');
      await expect(toolLinks).toHaveCount(10);
      const firstTool = await toolLinks.nth(0).boundingBox();
      const secondTool = await toolLinks.nth(1).boundingBox();
      expect(firstTool?.y).toBeCloseTo(secondTool!.y, 0);
      expect(secondTool!.x).toBeGreaterThan(firstTool!.x);
      await checkLayout(page, `${device}-all-navigation`, found);
      await menu.getByRole('link', { name: 'Overview', exact: true }).click();
      await expect(menu).toHaveCount(0);
      await expect(page).toHaveURL(/#\/overview$/);
    }
    expect(found, 'responsive layout issues').toEqual([]);
  });
}
