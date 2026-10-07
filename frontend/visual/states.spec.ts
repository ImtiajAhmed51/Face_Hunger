import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { test, type Page } from '@playwright/test';
import { EMPTY, OUT, SCREENS, VIEWPORTS, measure, prepare, settle, shotPath, type Theme } from './helpers';

// Interactive and edge states of the key screens, plus every screen on an empty first-run library.
const viewports = (process.env.VP ?? '390x844,1280x800').split(',');
const themes = (process.env.THEMES ?? 'light,dark').split(',') as Theme[];
const lang = (process.env.LANGS ?? 'en') as 'en' | 'bn';

type Step = { screen: string; state: string; run: (page: Page, phone: boolean) => Promise<void> };
const open = (path: string) => async (page: Page) => { await page.goto(`/#${path}`); await settle(page); };
const STEPS: Step[] = [
  { screen: 'photos', state: 'viewer', run: async (page) => { await open('/photos')(page); await page.locator('.image-button').first().click(); await settle(page); } },
  { screen: 'photos', state: 'selected', run: async (page) => { await open('/photos')(page); const boxes = page.locator('[role=gridcell] input[type=checkbox]'); await boxes.nth(0).check(); await boxes.nth(1).check(); await settle(page); } },
  { screen: 'photos', state: 'hover', run: async (page) => { await open('/photos')(page); await page.locator('.media-card').nth(1).hover(); await page.waitForTimeout(200); } },
  { screen: 'photos', state: 'focus', run: async (page) => { await open('/photos')(page); for (let i = 0; i < 4; i++) await page.keyboard.press('Tab'); } },
  { screen: 'photos', state: 'loading', run: async (page) => { await page.route('**/api/media?**', async (route) => { await new Promise((r) => setTimeout(r, 4000)); await route.continue(); }); await page.goto('/#/photos'); await page.waitForTimeout(900); } },
  { screen: 'photos', state: 'error', run: async (page) => { await page.route('**/api/media?**', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"The index is busy. Try again in a moment."}' })); await page.goto('/#/photos'); await page.waitForTimeout(2500); } },
  { screen: 'home', state: 'palette', run: async (page) => { await open('/')(page); await page.keyboard.press('ControlOrMeta+k'); await page.waitForTimeout(300); await page.keyboard.type('pho'); await page.waitForTimeout(200); } },
  { screen: 'home', state: 'menu', run: async (page, phone) => { await open('/')(page); if (phone) { await page.locator('header button').last().click(); await page.waitForTimeout(300); } } },
  { screen: 'person', state: 'dialog', run: async (page) => { await open('/people/3')(page); await page.getByRole('button', { name: 'Rename' }).click(); await page.waitForTimeout(300); } },
  { screen: 'person', state: 'menu', run: async (page) => { await open('/people/3')(page); await page.getByRole('button', { name: 'More actions' }).click(); await page.waitForTimeout(300); } },
  { screen: 'person', state: 'share-dialog', run: async (page) => { await open('/people/3')(page); await page.getByRole('button', { name: 'Encrypted export' }).click(); await page.waitForTimeout(300); } },
  { screen: 'storage', state: 'category', run: async (page) => { await open('/storage')(page); await page.getByRole('button', { name: /Largest files/ }).click(); await settle(page); } },
  { screen: 'storage', state: 'toast', run: async (page) => { await open('/storage')(page); await page.getByRole('button', { name: /Largest files/ }).click(); await settle(page); await page.locator('.storage-grid input[type=checkbox]').first().check(); await page.getByRole('button', { name: 'Move to Deleted' }).click(); await page.locator('.toast').first().waitFor({ timeout: 5000 }).catch(() => {}); } },
  { screen: 'search', state: 'results', run: async (page) => { await open('/search')(page); await page.locator('input').first().fill('Ada 2025'); await page.keyboard.press('Enter'); await settle(page); await page.waitForTimeout(600); } },
  { screen: 'duplicates', state: 'resolver', run: async (page) => { await open('/duplicates')(page); await page.getByRole('button', { name: 'Review and resolve' }).click(); await settle(page); } },
  { screen: 'videos', state: 'viewer', run: async (page) => { await open('/videos')(page); await page.locator('.image-button').first().click(); await settle(page); await page.waitForTimeout(600); } },
];

test('capture states', async ({ browser }) => {
  const findings: Record<string, string[]> = {};
  for (const theme of themes) for (const vp of viewports) {
    const [w, h] = VIEWPORTS[vp];
    for (const step of STEPS) {
      const context = await browser.newContext({ viewport: { width: w, height: h } });
      const page = await context.newPage();
      const problems: string[] = [];
      await prepare(page, theme, lang, problems);
      try { await step.run(page, w < 700); } catch (error) { problems.push(`step failed: ${String(error).slice(0, 160)}`); }
      await page.screenshot({ path: shotPath(step.screen, step.state, vp, theme, lang) });
      const facts = [...(step.state === 'error' ? [] : await measure(page)), ...problems.filter((p) => step.state !== 'error' || !p.includes('500'))];
      if (facts.length) findings[`${step.screen} ${step.state} ${vp} ${theme}`] = facts;
      if (step.state === 'toast') await page.keyboard.press('ControlOrMeta+z').catch(() => {});
      await page.waitForTimeout(step.state === 'toast' ? 800 : 0);
      await context.close();
    }
    // First run: every screen with nothing in the library.
    const context = await browser.newContext({ viewport: { width: w, height: h }, baseURL: EMPTY });
    const page = await context.newPage();
    const problems: string[] = [];
    await prepare(page, theme, lang, problems);
    for (const [screen, path] of Object.entries(SCREENS)) {
      if (/\/\d+$/.test(path)) continue;
      await page.goto(`${EMPTY}/#${path}`);
      await settle(page);
      await page.screenshot({ path: shotPath(screen, 'empty', vp, theme, lang) });
      const facts = [...await measure(page), ...problems.splice(0)];
      if (facts.length) findings[`${screen} empty ${vp} ${theme}`] = facts;
    }
    await context.close();
  }
  writeFileSync(join(OUT, 'findings-states.json'), JSON.stringify(findings, null, 1));
});
