import { defineConfig } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Visual QA: a photo-like fixture library on :8781 and an empty first-run data dir on :8782.
const root = join(tmpdir(), 'face-hunger-visual');
const empty = join(tmpdir(), 'face-hunger-visual-empty');
const python = process.env.LFS_PYTHON ?? 'python';
const env = (dir: string, port: number) =>
  `LFS_DATA_DIR=${dir}/data LFS_MODEL_DIR=${dir}/no-models LFS_ALLOWED_ROOTS=${dir} LFS_WATCH=false LFS_HOST=127.0.0.1 LFS_PORT=${port} LFS_LOG_LEVEL=WARNING`;

export default defineConfig({
  testDir: './visual',
  timeout: 600_000,
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  snapshotPathTemplate: '{testDir}/baselines/{arg}{ext}',
  expect: { toHaveScreenshot: { maxDiffPixelRatio: 0.002, animations: 'disabled', caret: 'hide' } },
  use: { baseURL: 'http://127.0.0.1:8781', trace: 'off' },
  projects: [
    { name: 'chromium', use: { browserName: 'chromium' } },
    { name: 'firefox', use: { browserName: 'firefox' }, grep: /@cross/ },
    { name: 'webkit', use: { browserName: 'webkit' }, grep: /@cross/ },
  ],
  webServer: [
    { command: `cd .. && ${python} scripts/make_visual_fixture.py --root ${root} && ${env(root, 8781)} ${python} -m backend`,
      url: 'http://127.0.0.1:8781/api/health', reuseExistingServer: false, timeout: 120_000 },
    { command: `cd .. && rm -rf ${empty} && mkdir -p ${empty} && ${env(empty, 8782)} ${python} -m backend`,
      url: 'http://127.0.0.1:8782/api/health', reuseExistingServer: false, timeout: 120_000 },
  ],
});
