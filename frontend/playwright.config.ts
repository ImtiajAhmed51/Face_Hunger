import { defineConfig } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Each run builds a fresh fixture library (scripts/make_ui_fixture.py) and serves the production
// build from an isolated data dir, so tests never touch a real library. The model dir is empty
// on purpose: results must not depend on which optional models are installed.
const root = join(tmpdir(), 'face-hunger-e2e');
const python = process.env.LFS_PYTHON ?? 'python';
const port = 8779;

export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    viewport: { width: 1280, height: 860 },
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: `cd .. && ${python} scripts/make_ui_fixture.py --root ${root} && ` +
      `LFS_DATA_DIR=${root}/data LFS_MODEL_DIR=${root}/no-models LFS_ALLOWED_ROOTS=${root} LFS_WATCH=false LFS_HOST=127.0.0.1 LFS_PORT=${port} ` +
      `LFS_LOG_LEVEL=WARNING ${python} -m backend`,
    cwd: '.',
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
