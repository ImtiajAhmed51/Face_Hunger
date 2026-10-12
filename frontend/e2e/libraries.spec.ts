import { copyFileSync, existsSync, mkdirSync, renameSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { expect, go, test } from './fixtures';

// The folder chooser, the connect-to-share dialog, adding a library, and offline/online.
// There is no SMB server here: the "share" is a folder next to the fixture library, and the one
// request that would mount a share is answered by the test with that folder's path.
const root = join(tmpdir(), 'face-hunger-e2e');
const share = join(root, 'Shares', 'Windows-PC', 'Family Photos');

test.beforeAll(() => {
  rmSync(join(root, 'Shares'), { recursive: true, force: true });
  rmSync(join(root, 'Shares-unplugged'), { recursive: true, force: true });
  mkdirSync(join(share, 'Holiday 2024', 'Beach'), { recursive: true });
  mkdirSync(join(share, 'Scans'), { recursive: true });
  mkdirSync(join(share, '$RECYCLE.BIN'), { recursive: true });
  mkdirSync(join(share, '@eaDir'), { recursive: true });
  copyFileSync(join(root, 'library', 'trip', 'TRIP_0.jpg'), join(share, 'Holiday 2024', 'a.jpg'));
  copyFileSync(join(root, 'library', 'trip', 'TRIP_1.jpg'), join(share, 'Holiday 2024', 'b.jpg'));
  copyFileSync(join(root, 'library', 'trip', 'TRIP_2.jpg'), join(share, 'Holiday 2024', 'Beach', 'c.jpg'));
});

test('libraries: choose a folder with mouse and keyboard, connect a share, add it, go offline and come back', async ({ page, errors }) => {
  const external: string[] = [];
  page.on('request', (req) => { if (!['127.0.0.1', 'localhost'].includes(new URL(req.url()).hostname) && !/^(data|blob|about):/.test(req.url())) external.push(req.url()); });
  let connectBody: Record<string, unknown> | null = null;
  await page.route('**/api/shares/connect', async (route) => {
    connectBody = route.request().postDataJSON();
    await route.fulfill({ json: { mount_point: share, protocol: 'smb', server: 'windows-pc', share: 'Family Photos', name: 'Family Photos', read_only: false, libraries: [], already_connected: false } });
  });
  // The fixture folder becomes a "Recent" place, so the test never browses the real home folder.
  await page.request.post('/api/fs/allow', { headers: { 'X-LFS-Request': '1' }, data: { paths: [join(root, 'library')] } });

  await go(page, '/libraries');
  await expect(page.getByRole('heading', { level: 1, name: 'Libraries' })).toBeVisible();
  await expect(page.getByRole('article', { name: 'UI fixture' })).toBeVisible();
  await page.getByRole('button', { name: 'Add library' }).click();
  const chooser = page.getByRole('dialog', { name: 'Choose a folder' });
  await expect(chooser.getByRole('navigation', { name: 'Places' })).toBeVisible();
  await expect(chooser.getByText('Network shares')).toBeVisible();

  // Mouse: a place in the sidebar, then a folder.
  await chooser.getByRole('navigation', { name: 'Places' }).getByRole('button', { name: 'library' }).click();
  const list = chooser.getByRole('group', { name: 'Folders' });
  await expect(list.locator('li.chooser-item')).toHaveText([/backup/, /camera/, /trip/]);
  await expect(list.locator('li.chooser-item').filter({ hasText: 'trip' })).toContainText(/\d+ photos · 0 videos/);
  await list.getByRole('button', { name: 'camera' }).click();
  await expect(chooser.getByRole('navigation', { name: 'Location' }).getByText('camera')).toBeVisible();
  await expect(chooser.getByText('There are no folders inside this one.')).toBeVisible();

  // Keyboard: Backspace goes up, arrows move, Enter opens, the filter narrows.
  await chooser.getByRole('button', { name: 'Go to the parent folder' }).focus();
  await page.keyboard.press('Backspace');
  await expect(list.locator('li.chooser-item')).toHaveCount(3);
  await list.focus();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await expect(list.locator('li.chooser-item').nth(2)).toHaveClass(/active/);
  await page.keyboard.press('Enter');
  await expect(chooser.getByRole('navigation', { name: 'Location' }).locator('strong')).toHaveText('trip');
  await page.keyboard.press('Backspace');
  await chooser.getByRole('searchbox', { name: 'Filter folders' }).fill('cam');
  await expect(list.locator('li.chooser-item')).toHaveText([/camera/]);

  // Connect to a network share: the password goes in the request body once and nowhere else.
  await chooser.getByRole('button', { name: 'Connect to network share' }).click();
  const connect = page.getByRole('dialog', { name: 'Connect to a network share' });
  await connect.getByLabel('Computer name or IP address').fill('WINDOWS-PC');
  await connect.getByLabel('Shared folder name').fill('Family Photos');
  await connect.getByLabel('User name').fill('imtiaj');
  await connect.getByLabel('Password').fill('fixture-password');
  await connect.getByRole('button', { name: 'Connect', exact: true }).click();
  await expect(connect).toBeHidden();
  expect(connectBody).toMatchObject({ protocol: 'smb', server: 'WINDOWS-PC', share: 'Family Photos', username: 'imtiaj', guest: false });
  // The chooser is now inside the share; system folders are hidden.
  await expect(list.locator('li.chooser-item')).toHaveText([/Holiday 2024/, /Scans/]);
  await expect(list.locator('li.chooser-item').filter({ hasText: 'Holiday 2024' })).toContainText('2 photos · 0 videos');
  await list.getByRole('button', { name: 'Holiday 2024' }).click();
  await expect(chooser.locator('.chooser-path')).toContainText('Shares/Windows-PC/Family Photos/Holiday 2024');
  await expect(chooser.locator('.chooser-summary')).toContainText('about 3 photos and 0 videos');
  await expect(chooser.locator('.chooser-summary')).toContainText('Readable');
  await chooser.getByRole('button', { name: 'Select this folder' }).click();
  await expect(chooser).toBeHidden();

  // The library exists; no path was typed anywhere.
  const card = page.getByRole('article', { name: 'Holiday 2024' });
  await expect(card).toBeVisible();
  await expect(card.getByText('Online')).toBeVisible();
  await expect(card.getByText('Not scanned yet')).toBeVisible();
  await expect(page.locator('input[placeholder*="path" i]')).toHaveCount(0);
  const libraries = await page.request.get('/api/libraries').then((r) => r.json());
  const added = libraries.items.find((l: { name: string }) => l.name === 'Holiday 2024');
  expect(added.path).toContain('Holiday 2024');

  // The network goes away: the library is offline, nothing is removed, a banner offers to reconnect.
  renameSync(join(root, 'Shares'), join(root, 'Shares-unplugged'));
  try {
    await card.getByRole('button', { name: 'Health' }).click();
    await card.getByRole('button', { name: 'Test connection' }).click();
    await expect(card.getByText('Not reachable right now.')).toBeVisible();
    await page.request.post(`/api/libraries/${added.id}/reconnect`, { headers: { 'X-LFS-Request': '1' } });
    await go(page, '/browse');
    const banner = page.locator('.offline-banner');
    await expect(banner).toContainText('“Holiday 2024” is offline. Everything already indexed stays searchable.');
    await expect(page.locator('[role=gridcell]').first()).toBeVisible(); // the rest of the library is untouched
    // Still unplugged: the answer comes back and the banner stays.
    await Promise.all([page.waitForResponse((r) => r.url().includes('/reconnect')), banner.getByRole('button', { name: 'Reconnect' }).click()]);
    await expect(banner.getByRole('button', { name: 'Reconnect' })).toBeEnabled();
    renameSync(join(root, 'Shares-unplugged'), join(root, 'Shares'));
    await banner.getByRole('button', { name: 'Reconnect' }).click();
    await expect(banner).toBeHidden();
  } finally {
    if (existsSync(join(root, 'Shares-unplugged'))) renameSync(join(root, 'Shares-unplugged'), join(root, 'Shares'));
  }
  await go(page, '/libraries');
  await expect(page.getByRole('article', { name: 'Holiday 2024' }).getByText('Online')).toBeVisible();

  // Remove it again (index only), so other tests see the fixture as it was.
  await page.request.delete(`/api/libraries/${added.id}`, { headers: { 'X-LFS-Request': '1' }, data: { confirm: 'REMOVE LIBRARY' } });
  expect(existsSync(join(share, 'Holiday 2024', 'a.jpg'))).toBe(true);
  expect(external).toEqual([]);
  expect(errors).toEqual([]);
});

test('libraries: the chooser shows a calm error for a slow or unreachable share, and the screen passes axe', async ({ page, errors, axe }) => {
  await page.request.post('/api/fs/allow', { headers: { 'X-LFS-Request': '1' }, data: { paths: [join(root, 'library')] } });
  await go(page, '/libraries');
  await axe(page, 'libraries');
  await page.getByRole('button', { name: 'Add library' }).click();
  const chooser = page.getByRole('dialog', { name: 'Choose a folder' });
  await chooser.getByRole('navigation', { name: 'Places' }).getByRole('button', { name: 'library' }).click();
  await expect(chooser.getByRole('group', { name: 'Folders' }).locator('li.chooser-item')).toHaveCount(3);
  await axe(page, 'folder chooser');
  await page.route('**/api/fs/list?**', (route) => route.fulfill({ status: 504, json: { detail: 'This share is slow or unreachable. Retry.' } }));
  await chooser.getByRole('group', { name: 'Folders' }).getByRole('button', { name: 'trip' }).click();
  await expect(chooser.getByRole('alert')).toContainText('This share is slow or unreachable. Retry.');
  await page.unroute('**/api/fs/list?**');
  errors.length = 0; // the 504 above is the point of this test
  await chooser.getByRole('button', { name: 'Try again' }).click();
  await expect(chooser.getByRole('alert')).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(chooser).toBeHidden();
  errors.length = 0; // only the injected 504 was logged
});
