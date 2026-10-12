import { execFileSync } from 'node:child_process';
import { expect, go, test } from './fixtures';

test('legacy AVI converts only after explicit confirmation and preserves its source', async ({ page }) => {
  test.setTimeout(120_000);
  const python = process.env.LFS_PYTHON || '.venv/bin/python';
  const run = (script: string) => execFileSync(python, ['-c', script], { cwd: '..', encoding: 'utf8' });
  const saved = JSON.parse(run(`import sqlite3, tempfile, json, subprocess
from pathlib import Path
from backend.media_processing import _ffmpeg_bin
root=Path(tempfile.gettempdir())/'face-hunger-e2e'
conn=sqlite3.connect(root/'data/index.sqlite')
row=conn.execute("SELECT id,path,name FROM media WHERE kind='video' ORDER BY id LIMIT 1").fetchone()
source=root/'library'/'LEGACY_TEST.avi'
source.parent.mkdir(parents=True,exist_ok=True)
subprocess.run([_ffmpeg_bin(),'-v','error','-y','-f','lavfi','-i','testsrc2=size=160x120:rate=25','-t','2','-c:v','mpeg4',str(source)],check=True)
conn.execute('UPDATE media SET path=?,name=? WHERE id=?',(str(source),source.name,row[0]));conn.commit()
print(json.dumps({'id':row[0],'path':row[1],'name':row[2],'source':str(source)}))`));
  try {
    const conversions: string[] = [];
    page.on('request', request => {
      if (request.method() === 'POST' && /\/convert(?:\?|$)/.test(request.url())) conversions.push(request.url());
    });
    await go(page, '/videos');
    await page.locator('main').getByRole('button', { name: 'Open LEGACY_TEST.avi', exact: true }).click();
    await expect(page.locator('.conversion-panel').getByRole('button', { name: 'Convert for browser', exact: true })).toBeVisible();
    await page.waitForTimeout(1200);
    expect(conversions).toEqual([]);
    await page.locator('.conversion-panel').getByRole('button', { name: 'Convert for browser', exact: true }).click();
    expect(conversions).toEqual([]);
    await page.getByRole('dialog', { name: 'Convert this video for browser playback?' })
      .getByRole('button', { name: 'Convert video', exact: true }).click();
    const video = page.locator('.viewer-dialog video');
    await expect.poll(async () => video.count() ? video.evaluate((v: HTMLVideoElement) => Number.isFinite(v.duration) && v.duration > 0) : false,
      { timeout: 90_000 }).toBe(true);
    await video.evaluate((v: HTMLVideoElement) => v.play());
    await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime)).toBeGreaterThan(0.1);
    expect(conversions).toHaveLength(1);
    await page.getByRole('button', { name: 'Remove converted copy', exact: true }).click();
    await page.getByRole('dialog', { name: 'Remove converted playback copy?' })
      .getByRole('button', { name: 'Remove copy', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Remove converted copy', exact: true })).toHaveCount(0);
    await expect(page.locator('.conversion-panel').getByRole('button', { name: 'Convert for browser', exact: true })).toBeVisible();
    expect(conversions).toHaveLength(1);
    const integrity = JSON.parse(run(`import sqlite3,tempfile,json
from pathlib import Path
root=Path(tempfile.gettempdir())/'face-hunger-e2e'
conn=sqlite3.connect(root/'data/index.sqlite')
print(json.dumps({'path':conn.execute('SELECT path FROM media WHERE id=?',(${saved.id},)).fetchone()[0], 'source_exists':Path(${JSON.stringify(saved.source)}).is_file()}))`));
    expect(integrity.path).toBe(saved.source);
    expect(integrity.source_exists).toBe(true);
  } finally {
    run(`import sqlite3,tempfile
from pathlib import Path
conn=sqlite3.connect(Path(tempfile.gettempdir())/'face-hunger-e2e/data/index.sqlite')
conn.execute('UPDATE media SET path=?,name=? WHERE id=?',(${JSON.stringify(saved.path)},${JSON.stringify(saved.name)},${saved.id}));conn.commit()`);
  }
});
