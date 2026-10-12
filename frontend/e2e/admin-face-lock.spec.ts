import { execFileSync } from 'node:child_process';
import { expect, test } from './fixtures';

test.use({ launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] } });

test('admin camera failure stays locked and emergency password recovers access', async ({ page }) => {
  const password = 'fixture-admin-recovery';
  const headers = { 'X-LFS-Request': '1' };
  const setup = await page.request.put('/api/lock/password', { headers, data: { password } });
  expect(setup.ok()).toBe(true);
  const python = process.env.LFS_PYTHON || '.venv/bin/python';
  // Only the disposable fixture database; never register a synthetic admin in a real library.
  execFileSync(python, ['-c', `import sqlite3,tempfile,json
from pathlib import Path
conn=sqlite3.connect(Path(tempfile.gettempdir())/'face-hunger-e2e/data/index.sqlite')
value=json.dumps({'version':1,'contract':{'model':'fixture'},'dimension':512,'threshold':0.7,'vectors':[[1.0]+[0.0]*511]})
conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('admin_face_lock',?)",(value,));conn.commit()`], { cwd: '..' });
  try {
    await page.request.post('/api/lock/lock', { headers });
    await page.goto('/#/photos');
    await expect(page.locator('.lock-screen')).toBeVisible();
    expect((await page.request.get('/api/media')).status()).toBe(401);
    await page.getByRole('button', { name: 'Open camera', exact: true }).click();
    await expect.poll(() => page.locator('.face-unlock-camera video').evaluate((v: HTMLVideoElement) => v.videoWidth)).toBeGreaterThan(0);
    await page.getByRole('button', { name: 'Unlock with my face', exact: true }).click();
    await expect(page.getByRole('alert')).toContainText('model unavailable');
    await expect(page.locator('.lock-screen')).toBeVisible();
    expect((await page.request.get('/api/media')).status()).toBe(401);
    await page.getByRole('button', { name: 'Close camera', exact: true }).click();
    await page.locator('#lock-password').fill(password);
    await page.locator('.lock-card').getByRole('button', { name: 'Unlock', exact: true }).click();
    await expect(page.locator('.lock-screen')).toHaveCount(0);
    await expect(page.locator('main h1')).toBeVisible();
  } finally {
    await page.request.post('/api/lock/unlock', { headers, data: { password } });
    await page.request.delete('/api/lock/password', { headers, data: { password } });
  }
});
