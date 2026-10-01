import { expect, go, test } from './fixtures';

test('timeline: sections, sticky date and keyboard scrubber', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/timeline');
  await expect(page.getByRole('heading', { level: 1, name: 'Timeline' })).toBeVisible();
  await expect(page.locator('.timeline-header').first()).toBeVisible();
  const scrubber = page.getByRole('slider', { name: 'Jump to date' });
  await scrubber.focus();
  await page.keyboard.press('End');
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(200);
  await expect(scrubber).toHaveAttribute('aria-valuetext', /\d{4}/);
  await page.keyboard.press('Home');
  await axe(page, 'timeline');
});

test('map: clusters with tiles off, no external requests, cluster filters the grid', async ({ page, errors, axe }) => {
  void errors;
  const external: string[] = [];
  page.on('request', (req) => {
    const url = new URL(req.url());
    if (!['127.0.0.1', 'localhost'].includes(url.hostname) && !['data:', 'blob:'].includes(url.protocol)) external.push(req.url());
  });
  await go(page, '/map');
  await expect(page.locator('.map-canvas[data-render-ms]')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/No base map: nothing is downloaded/)).toBeVisible();
  await page.locator('.map-places button').first().click();
  await expect(page.getByText(/items here/).first()).toBeVisible();
  await expect(page.locator('section.collection [role=gridcell]').first()).toBeVisible();
  expect(external).toEqual([]);
  await axe(page, 'map');
});

test('events: list, detail, rename and undo', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/events');
  await expect(page.getByRole('heading', { level: 1, name: 'Events' })).toBeVisible();
  await expect(page.locator('.event-card').first()).toBeVisible({ timeout: 15_000 });
  await axe(page, 'events');
  await page.locator('.event-card a.event-cover').first().click();
  await expect(page.getByRole('button', { name: 'Rename' })).toBeVisible();
  const original = await page.getByRole('heading', { level: 1 }).textContent();
  await page.getByRole('button', { name: 'Rename' }).click();
  await page.getByLabel('Rename').fill('Playwright trip');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('heading', { level: 1, name: 'Playwright trip' })).toBeVisible();
  await page.locator('body').click({ position: { x: 5, y: 5 } });
  await page.keyboard.press('ControlOrMeta+z');
  await expect(page.getByRole('heading', { level: 1, name: original ?? '' })).toBeVisible();
  await axe(page, 'event detail');
});

test('albums: create from a selection and open it', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/photos');
  const boxes = page.locator('.virtual-cell input[type=checkbox]');
  await boxes.nth(0).check();
  await boxes.nth(1).check();
  await page.getByRole('button', { name: 'Add to album' }).click();
  await page.getByPlaceholder('Album name').fill('E2E album');
  await page.getByRole('button', { name: 'Create' }).click();
  await expect(page.locator('.toast').filter({ hasText: 'Created album E2E album' })).toBeVisible();
  await go(page, '/albums');
  await expect(page.locator('.album-tile', { hasText: 'E2E album' })).toContainText('2 items');
  await axe(page, 'albums');
  await page.locator('.album-tile', { hasText: 'E2E album' }).click();
  await expect(page.getByRole('heading', { level: 1, name: 'E2E album' })).toBeVisible();
  await expect(page.locator('[role=gridcell]')).toHaveCount(2);
  await axe(page, 'album detail');
});

test('memories on home', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/');
  const memories = page.locator('.memories');
  await expect(memories.getByRole('heading', { name: 'Memories' })).toBeVisible();
  await expect(memories.getByText('2 years ago today')).toBeVisible();
  await expect(memories.locator('.memory-strip button')).toHaveCount(3);
  await axe(page, 'home');
});

test('video viewer: person lanes, chips seek the player', async ({ page, errors, axe }) => {
  void errors;
  await go(page, '/videos');
  await page.getByRole('button', { name: /^Open CLIP_0001/ }).click();
  const insights = page.locator('.video-insights');
  await expect(insights.getByText('Who appears, and when')).toBeVisible();
  await expect(insights.locator('.video-lane')).toHaveCount(2);
  const video = page.locator('dialog[open] video');
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState)).toBeGreaterThan(0);
  await insights.getByRole('button', { name: 'Jump to Ben at 0:09' }).click();
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => Math.round(v.currentTime))).toBeGreaterThanOrEqual(9);
  await video.evaluate((v: HTMLVideoElement) => v.pause());
  await axe(page, 'video viewer');
});

test('duplicates: keyboard-only resolve and undo', async ({ page, errors }) => {
  void errors;
  await go(page, '/duplicates');
  await page.getByRole('button', { name: 'Review and resolve' }).focus();
  await page.keyboard.press('Enter');
  await expect(page.getByText(/Group 1 of 20/)).toBeVisible();
  for (const key of ['ArrowRight', 'ArrowRight', 'Tab', 'f']) await page.keyboard.press(key);
  await expect(page.getByText(/Group 3 of 20/)).toBeVisible();
  await page.keyboard.press('r');
  await expect(page.locator('.toast').filter({ hasText: 'Resolved duplicates' })).toBeVisible();
  const bin = await page.request.get('/api/duplicates/bin').then((r) => r.json());
  expect(bin.files).toBe(27);
  await page.keyboard.press('ControlOrMeta+z');
  await expect.poll(async () => (await page.request.get('/api/duplicates/bin').then((r) => r.json())).files).toBe(0);
});

test('reduced motion: no running animations on new screens', async ({ page, errors }) => {
  void errors;
  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const hash of ['/timeline', '/events', '/albums', '/']) {
    await go(page, hash);
    await page.waitForTimeout(300);
    const running = await page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running').length);
    expect(running, hash).toBe(0);
  }
});

test('bangla: new screens switch language', async ({ page, errors, axe }) => {
  void errors;
  await page.addInitScript(() => localStorage.setItem('lfs-lang', 'bn'));
  await go(page, '/timeline');
  await expect(page.getByRole('heading', { level: 1, name: 'টাইমলাইন' })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'bn');
  await go(page, '/events');
  await expect(page.getByRole('heading', { level: 1, name: 'ইভেন্ট' })).toBeVisible();
  await go(page, '/albums');
  await expect(page.getByRole('heading', { level: 1, name: 'অ্যালবাম' })).toBeVisible();
  await axe(page, 'albums (bn)');
});
