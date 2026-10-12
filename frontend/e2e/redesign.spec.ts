import { expect, go, test } from './fixtures';
import { mkdirSync } from 'node:fs';

const shots = 'test-results/redesign';

test('photos shell: timeline, filters, viewer, zoom, paging, sidebar and refresh', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/photos');
  await expect(page.getByRole('heading', { name: 'Photos', exact: true, level: 1 })).toBeVisible();
  const navigation = page.locator('.sidebar');
  await expect(navigation.getByRole('link', { name: 'Favorites', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Collapse sidebar' }).click();
  await expect(page.locator('html')).toHaveClass(/sidebar-collapsed/);
  await page.reload();
  await expect(page.locator('html')).toHaveClass(/sidebar-collapsed/);
  await page.getByRole('button', { name: 'Expand sidebar' }).click();
  await page.setViewportSize({ width: 900, height: 860 });
  await expect.poll(async () => (await navigation.boundingBox())!.width).toBe(232);
  await page.getByRole('button', { name: 'Collapse sidebar' }).click();
  await expect.poll(async () => (await navigation.boundingBox())!.width).toBe(76);
  await page.getByRole('button', { name: 'Expand sidebar' }).click();
  await expect.poll(async () => (await navigation.boundingBox())!.width).toBe(232);
  await page.setViewportSize({ width: 1280, height: 860 });
  await page.getByRole('group', { name: 'Media type' }).getByRole('button', { name: 'Videos', exact: true }).click();
  await expect(page.locator('.timeline-tile')).toHaveCount(1);
  await page.getByRole('group', { name: 'Media type' }).getByRole('button', { name: 'Photos', exact: true }).click();
  await expect(page.locator('.timeline-tile').first()).toBeVisible();
  const name = await page.locator('.timeline-tile').first().getAttribute('aria-label');
  await page.locator('.timeline-tile').first().click();
  const viewer = page.locator('dialog.viewer-dialog');
  await expect(viewer.getByRole('heading', { level: 2 })).toContainText(name!.split(',')[0]);
  const fittedWidth = (await viewer.locator('.viewer-photo').boundingBox())!.width;
  await viewer.getByRole('button', { name: 'Zoom in', exact: true }).click();
  await expect.poll(async () => (await viewer.locator('.viewer-photo').boundingBox())!.width).toBeGreaterThan(fittedWidth);
  await expect(viewer.getByRole('button', { name: 'Fit photo to screen' })).toHaveText('150%');
  await viewer.getByRole('button', { name: 'Fit photo to screen' }).click();
  await expect(viewer.getByRole('button', { name: 'Fit photo to screen' })).toHaveText('100%');
  await viewer.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(viewer.getByRole('heading', { level: 2 })).not.toContainText(name!.split(',')[0]);
  await viewer.getByRole('button', { name: 'Previous', exact: true }).click();
  await expect(viewer.getByRole('heading', { level: 2 })).toContainText(name!.split(',')[0]);
  await page.keyboard.press('Escape');
  await expect(viewer).toHaveCount(0);
  await axe(page, 'photos timeline');
  mkdirSync(shots, { recursive: true });
  const desktopHeader = await page.locator('.page-header').boundingBox();
  const desktopRail = await page.getByRole('slider', { name: 'Jump to date' }).boundingBox();
  expect(desktopRail!.y).toBeGreaterThan(desktopHeader!.y + desktopHeader!.height);
  await page.screenshot({ path: `${shots}/photos-desktop.png` });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => {
    const header = (await page.locator('.page-header').boundingBox())!;
    const rail = (await page.getByRole('slider', { name: 'Jump to date' }).boundingBox())!;
    return rail.y - header.y - header.height;
  }).toBeGreaterThan(0);
  await page.screenshot({ path: `${shots}/photos-mobile.png` });
  await page.getByRole('button', { name: 'More pages' }).click();
  const menu = page.getByRole('dialog', { name: 'Your collection' });
  await expect(menu).toBeVisible();
  await menu.getByRole('link', { name: 'Favorites', exact: true }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Favorites' })).toBeVisible();
});

test('person faces: one list request, selection, bulk confirm, reject, split and undo', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/people/1');
  await page.getByRole('button', { name: 'Individual faces' }).click();
  await expect(page.locator('.face-tile').first()).toBeVisible();
  await page.getByLabel('Select faces on page').check();
  const ids = await page.locator('.face-tile input').evaluateAll(elements => elements.map(e => Number(e.getAttribute('aria-label')?.match(/face (\d+)/)?.[1])));
  const response = page.waitForResponse(r => r.url().endsWith('/api/faces/review') && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Confirm', exact: true }).click();
  expect((await response).status()).toBe(200);
  await expect.poll(async () => (await page.request.get('/api/people/1/faces').then(r => r.json())).items
    .filter((f: { id: number }) => ids.includes(f.id)).every((f: { review_state: string }) => f.review_state === 'confirmed')).toBe(true);
  await expect(page.getByRole('button', { name: 'Confirm', exact: true })).toHaveCount(0);
  await page.getByLabel('Select faces on page').check();
  await page.getByRole('button', { name: 'Not them', exact: true }).click();
  await expect.poll(async () => (await page.request.get('/api/people/1/faces').then(r => r.json())).items
    .filter((f: { id: number }) => ids.includes(f.id)).every((f: { review_state: string }) => f.review_state === 'rejected')).toBe(true);
  await expect(page.getByRole('button', { name: 'Not them', exact: true })).toHaveCount(0);
  await page.locator('.face-tile .image-button').first().focus();
  await page.keyboard.press('ControlOrMeta+z');
  await expect.poll(async () => (await page.request.get('/api/people/1/faces').then(r => r.json())).items
    .filter((f: { id: number }) => ids.includes(f.id)).every((f: { review_state: string }) => f.review_state === 'confirmed')).toBe(true);
  await page.locator('.face-tile input').first().check();
  await page.getByRole('button', { name: 'Move / split' }).click();
  const dialog = page.getByRole('dialog', { name: /Move 1 selected face/ });
  await dialog.getByRole('button', { name: 'Split into new person' }).click();
  await dialog.getByPlaceholder('Leave blank for an unnamed person').fill('Audit split');
  await dialog.getByRole('button', { name: 'Create person & move' }).click();
  await expect(dialog).toHaveCount(0);
  await page.locator('.face-tile .image-button').first().focus();
  await page.keyboard.press('ControlOrMeta+z');
  await expect.poll(async () => (await page.request.get('/api/people/1/faces').then(r => r.json())).total).toBe(ids.length);
  await axe(page, 'individual faces');
  mkdirSync(shots, { recursive: true });
  await page.screenshot({ path: `${shots}/person-faces.png` });
});

test('metadata search, favorites, deleted media restore and filters persist after refresh', async ({ page, errors }) => {
  void errors;
  await go(page, '/browse');
  await page.getByRole('searchbox', { name: 'Filter media by filename' }).fill('MEMORY_0');
  await expect(page.getByRole('button', { name: /^Open MEMORY_0/ })).toBeVisible();
  await page.getByRole('button', { name: /^Open MEMORY_0/ }).click();
  const viewer = page.locator('.viewer-dialog');
  await viewer.getByRole('button', { name: 'Favorite', exact: true }).click();
  await expect(viewer.getByRole('button', { name: 'Remove favorite', exact: true })).toBeVisible();
  await page.keyboard.press('Escape');
  await go(page, '/favorites');
  await expect(page.getByRole('button', { name: /^Open MEMORY_0/ })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: /^Open MEMORY_0/ })).toBeVisible();
  await go(page, '/search?q=MEMORY_0');
  await expect(page.locator('#natural-query')).toHaveValue('MEMORY_0');
  await expect(page.getByRole('button', { name: /^Open MEMORY_0/ })).toBeVisible();
  await page.locator('.virtual-cell input[type=checkbox]').first().check();
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete', exact: true }).click();
  await go(page, '/deleted');
  await expect(page.getByRole('button', { name: /^Open MEMORY_0/ })).toBeVisible();
  await page.locator('.virtual-cell input[type=checkbox]').first().check();
  await page.getByRole('button', { name: 'Restore', exact: true }).click();
  await go(page, '/favorites');
  await expect(page.getByRole('button', { name: /^Open MEMORY_0/ })).toBeVisible();
});


test('viewer crosses page boundaries in the complete media order', async ({ page, errors }) => {
  test.setTimeout(120_000);
  void errors;
  await go(page, '/browse');
  const listing = await page.request.get('/api/media?sort=date&limit=120').then(r => r.json());
  expect(listing.total).toBeGreaterThan(120);
  await page.getByRole('button', { name: /^Open / }).first().click();
  const viewer = page.locator('.viewer-dialog');
  for (let i = 1; i <= 121; i++) {
    const response = page.waitForResponse(r => /\/api\/media\/\d+$/.test(r.url()) && r.request().method() === 'GET');
    await viewer.getByRole('button', { name: 'Next', exact: true }).click();
    await response;
  }
  const second = await page.request.get('/api/media?sort=date&page=2&limit=120').then(r => r.json());
  await expect(viewer.getByRole('heading', { level: 2 })).toContainText(second.items[1].name);
  await viewer.getByRole('button', { name: 'Previous', exact: true }).click();
  await expect(viewer.getByRole('heading', { level: 2 })).toContainText(second.items[0].name);
});

test('major screens in both themes at desktop and phone widths', async ({ page, errors, axe }) => {
  void errors;
  test.setTimeout(180_000);
  mkdirSync(shots, { recursive: true });
  for (const theme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: theme });
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 844 });
      for (const route of ['/photos', '/people', '/search', '/albums', '/settings', '/review']) {
        await go(page, route);
        await expect(page.locator('h1')).toBeVisible();
        expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
        await page.screenshot({ path: `${shots}/${route.slice(1)}-${theme}-${width}.png` });
      }
    }
  }
  await axe(page, 'phone review');
});

test('review keyboard decisions, undo and source viewer', async ({ page, errors, axe }) => {
  void errors;
  for (const id of [4, 8]) await page.request.post(`/api/faces/${id}/review`, { data: { decision: 'reset' } });
  await go(page, '/review');
  await expect(page.locator('.review-workbench')).toBeVisible();
  await page.getByRole('button', { name: 'Open source media for this face' }).focus();
  await page.keyboard.press('v');
  await expect(page.locator('.viewer-dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  const yes = page.waitForResponse(r => /\/faces\/\d+\/review$/.test(r.url()) && r.request().postDataJSON()?.decision === 'yes');
  await page.keyboard.press('y');
  expect((await yes).status()).toBe(200);
  await page.getByRole('button', { name: 'Undo last decision' }).click();
  await expect(page.locator('.review-workbench')).toBeVisible();
  const no = page.waitForResponse(r => /\/faces\/\d+\/review$/.test(r.url()) && r.request().postDataJSON()?.decision === 'no');
  await page.getByRole('button', { name: /No, not them/ }).click();
  expect((await no).status()).toBe(200);
  await page.getByRole('button', { name: 'Undo last decision' }).click();
  await axe(page, 'review decisions');
});

test('people rename persists, merge and undo preserve both assignments', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/people/1');
  await page.getByRole('button', { name: 'Rename', exact: true }).click();
  await page.getByRole('dialog').getByLabel('Name', { exact: true }).fill('Ada audit');
  await page.getByRole('button', { name: 'Save name' }).click();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Ada audit', level: 1 })).toBeVisible();
  await page.getByRole('button', { name: 'Merge', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Merge Ada audit' });
  await dialog.getByRole('button', { name: /Ben/ }).click();
  await expect(dialog.locator('.merge-confidence')).toBeVisible();
  await dialog.getByRole('button', { name: 'Merge people' }).click();
  await expect(page.getByRole('heading', { name: 'Ben', level: 1 })).toBeVisible();
  await page.getByRole('button', { name: 'Rename', exact: true }).focus();
  await page.keyboard.press('ControlOrMeta+z');
  await expect.poll(async () => (await page.request.get('/api/people/1/faces').then(r => r.json())).total).toBe(4);
  await expect.poll(async () => (await page.request.get('/api/people/2/faces').then(r => r.json())).total).toBe(4);
  await page.request.patch('/api/people/1', { data: { name: 'Ada' } });
  await axe(page, 'merge undo');
});

test('save a filename search as a collection, open media and repeat a reset deep link', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/search?q=MEMORY_0');
  await expect(page.getByRole('button', { name: /^Open MEMORY_0/ })).toBeVisible();
  await page.getByRole('button', { name: 'Save search', exact: true }).click();
  await page.getByRole('dialog').getByLabel('Name', { exact: true }).fill('Audit memories');
  await page.getByRole('dialog').getByRole('button', { name: 'Save', exact: true }).click();
  await go(page, '/albums');
  await page.locator('.album-tile', { hasText: 'Audit memories' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'Audit memories' })).toBeVisible();
  await page.getByRole('button', { name: /^Open MEMORY_0/ }).click();
  await expect(page.locator('.viewer-dialog').getByRole('heading', { level: 2 })).toContainText('MEMORY_0');
  await page.keyboard.press('Escape');
  await axe(page, 'collection detail');
  await go(page, '/search?q=MEMORY_0');
  await page.getByRole('button', { name: 'Reset search' }).click();
  await page.locator('.topbar-search input').fill('MEMORY_0');
  await page.locator('.topbar-search input').press('Enter');
  await expect(page.getByRole('button', { name: /^Open MEMORY_0/ })).toBeVisible();
});

test('a newer deep link wins while the previous search is still parsing', async ({ page, errors }) => {
  void errors;
  await page.route('**/api/search/parse', async route => {
    if (route.request().postDataJSON()?.query === 'MEMORY_0') {
      await new Promise(resolve => setTimeout(resolve, 700));
    }
    await route.continue();
  });
  await page.goto('/#/search?q=MEMORY_0');
  await expect(page.getByRole('button', { name: 'Reading your search...' })).toBeVisible();
  await page.locator('.topbar-search input').fill('MEMORY_1');
  await page.locator('.topbar-search input').press('Enter');
  await expect(page.getByRole('button', { name: /^Open MEMORY_1/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /^Open MEMORY_0/ })).toHaveCount(0);
});

test('clusters zoom/search and cleanup category controls', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/clusters');
  const reset = page.getByRole('button', { name: 'Reset zoom' });
  await page.getByRole('button', { name: 'Zoom in', exact: true }).click();
  expect(await reset.textContent()).not.toContain('100%');
  await reset.click();
  await expect(reset).toHaveText('100%');
  await page.getByRole('button', { name: 'Zoom out', exact: true }).click();
  await reset.click();
  await page.getByRole('searchbox', { name: 'Search clusters by name' }).fill('no-such-person');
  await expect(page.getByRole('heading', { name: 'No cluster by that name' })).toBeVisible();
  await page.getByRole('button', { name: 'Clear search' }).click();
  await expect(page.getByRole('heading', { name: 'No cluster by that name' })).toHaveCount(0);
  await go(page, '/cleanup');
  const tabs = page.getByRole('group', { name: 'Cleanup category' });
  for (const label of ['Failed files', 'Missing files', 'Deleted records', 'Kept originals', 'Conversion backups', 'Duplicate people']) {
    const button = tabs.getByRole('button', { name: label, exact: true });
    await button.click();
    await expect(button).toHaveAttribute('aria-pressed', 'true');
  }
  await axe(page, 'cleanup controls');
});

test('timeline loading, empty and failed response retries', async ({ page }) => {
  // These intentionally injected API failures are expected; validate the rendered recovery UI.
  const exceptions: string[] = [];
  page.on('pageerror', e => exceptions.push(e.message));
  await page.route('**/api/timeline', async route => {
    await new Promise(resolve => setTimeout(resolve, 700));
    await route.fulfill({ json: { days: [], total: 0, undated: 0, guessed: 0 } });
  });
  await page.goto('/#/photos');
  await expect(page.getByRole('status', { name: 'Loading your timeline' })).toBeVisible();
  await expect(page.locator('.empty-state')).toBeVisible();
  await page.unroute('**/api/timeline');
  await page.route('**/api/timeline', route => route.fulfill({ status: 503, json: { detail: 'Temporary index unavailable' } }));
  await page.reload();
  await expect(page.getByRole('alert').filter({ hasText: 'Temporary index unavailable' })).toBeVisible();
  await page.unroute('**/api/timeline');
  await page.getByRole('button', { name: 'Try again', exact: true }).click();
  await expect(page.locator('.timeline-tile').first()).toBeVisible();
  expect(exceptions).toEqual([]);
});

test('command palette opens a real destination with keyboard navigation', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/photos');
  await page.getByRole('button', { name: /Quick actions/ }).click();
  const input = page.getByRole('combobox', { name: 'Type a command, page, person or search' });
  await input.fill('Libraries');
  await expect(page.getByRole('option', { name: /Libraries/ }).first()).toBeVisible();
  await axe(page, 'command palette');
  await input.press('Enter');
  await expect(page).toHaveURL(/#\/libraries$/);
  await expect(page.locator('.command-palette')).toHaveCount(0);
});

test('health integrity job, backup download and restore confirmation cancellation', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/health');
  const check = page.waitForResponse(r => r.url().endsWith('/api/health/check') && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Run integrity check', exact: true }).click();
  expect((await check).status()).toBe(200);
  await expect(page.getByRole('button', { name: 'Run integrity check', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Create backup', exact: true }).click();
  await expect(page.locator('.health-backups').getByRole('link', { name: 'Download' }).first()).toBeVisible({ timeout: 30_000 });
  const download = page.waitForEvent('download');
  await page.locator('.health-backups').getByRole('link', { name: 'Download' }).first().click();
  expect((await download).suggestedFilename()).toMatch(/\.zip$/);
  await page.locator('.health-backups').getByRole('button', { name: 'Restore…' }).first().click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await axe(page, 'health backups');
});
