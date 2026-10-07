import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium } from '@playwright/test';
import { expect, test } from './fixtures';

// Lighthouse on the core screens (desktop preset, production build). Slow, so opt in:
//   LIGHTHOUSE=1 npx playwright test e2e/lighthouse.spec.ts
const CORE = ['/', '/photos', '/people', '/search', '/timeline'];

test('lighthouse: core screens score at least 95 in every category', async ({ baseURL }) => {
  test.skip(!process.env.LIGHTHOUSE, 'set LIGHTHOUSE=1 to run');
  test.setTimeout(600_000);
  const out = mkdtempSync(join(tmpdir(), 'fh-lighthouse-'));
  const scores: Record<string, Record<string, number>> = {};
  const metrics: Record<string, Record<string, string>> = {};
  const shifts: Record<string, string[]> = {};
  for (const screen of CORE) {
    const file = join(out, `${screen.replace(/\W/g, '') || 'home'}.json`);
    execFileSync('npx', ['lighthouse', `${baseURL}/#${screen}`, '--preset=desktop', '--quiet', '--output=json', `--output-path=${file}`,
      '--only-categories=performance,accessibility,best-practices', '--chrome-flags=--headless=new --no-sandbox'],
      { env: { ...process.env, CHROME_PATH: chromium.executablePath() }, stdio: 'pipe' });
    const report = JSON.parse(readFileSync(file, 'utf8'));
    metrics[screen] = Object.fromEntries(['first-contentful-paint', 'largest-contentful-paint', 'total-blocking-time', 'cumulative-layout-shift', 'speed-index'].map((k) => [k, report.audits[k]?.displayValue]));
    shifts[screen] = (report.audits['layout-shifts']?.details?.items ?? []).slice(0, 4).map((i: { node?: { selector?: string }; score?: number }) => `${i.node?.selector} ${i.score?.toFixed(3)}`);
    scores[screen] = Object.fromEntries(Object.entries(report.categories as Record<string, { score: number }>).map(([k, v]) => [k, Math.round(v.score * 100)]));
  }
  console.log('LIGHTHOUSE', JSON.stringify(scores));
  console.log('METRICS', JSON.stringify(metrics));
  console.log('SHIFTS', JSON.stringify(shifts));
  for (const [screen, result] of Object.entries(scores))
    for (const [category, score] of Object.entries(result)) expect(score, `${screen} ${category}`).toBeGreaterThanOrEqual(95);
});
