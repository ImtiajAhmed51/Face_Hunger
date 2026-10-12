import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { expect, go, test } from './fixtures';

test('viewer fullscreen, face overlay toggle, metadata and original export', async ({ page, errors }) => {
  void errors;
  const listing = await page.request.get('/api/media?sort=date&limit=1').then(response => response.json());
  const expected = await page.request.get(`/api/media/${listing.items[0].id}/file`).then(response => response.body());
  await go(page, '/photos');
  await page.locator('.timeline-tile').first().click();
  const viewer = page.locator('.viewer-dialog');
  await expect(viewer.getByRole('heading', { name: 'File details' })).toBeVisible();
  const boxes = viewer.getByRole('button', { name: 'Face boxes', exact: true });
  const before = await boxes.getAttribute('aria-pressed');
  await boxes.click();
  await expect(boxes).toHaveAttribute('aria-pressed', before === 'true' ? 'false' : 'true');
  await viewer.getByRole('button', { name: 'Toggle fullscreen (F)' }).click();
  await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(true);
  await viewer.getByRole('button', { name: 'Toggle fullscreen (F)' }).click();
  await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(false);
  const downloaded = page.waitForEvent('download');
  await viewer.getByRole('button', { name: 'Export original + manifest', exact: true }).click();
  const file = await (await downloaded).path();
  expect(file).not.toBeNull();
  const python = resolve('..', process.env.LFS_PYTHON ?? '.venv/bin/python');
  const exported = execFileSync(python, ['-c',
    'import json,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); m=json.loads(z.read("manifest.json")); sys.stdout.buffer.write(z.read(m[0]["archive"]))', file!]);
  expect(exported).toEqual(expected);
});

test('recently added opens the indexed order and native video keyboard controls play and pause', async ({ page, errors }) => {
  void errors;
  const listing = await page.request.get('/api/media?sort=indexed&limit=120').then(response => response.json());
  await go(page, '/recently-added');
  await page.getByRole('button', { name: /^Open / }).first().click();
  await expect(page.locator('.viewer-dialog').getByRole('heading', { level: 2 })).toHaveText(listing.items[0].name);
  await page.keyboard.press('Escape');
  await go(page, '/videos');
  await page.getByRole('button', { name: /^Open / }).first().click();
  const video = page.locator('.viewer-dialog video');
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.readyState)).toBeGreaterThan(1);
  await video.focus();
  await video.press('Space');
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.paused)).toBe(false);
  await video.press('Space');
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.paused)).toBe(true);
});
