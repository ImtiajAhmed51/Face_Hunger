import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { test } from '@playwright/test';
import { OUT, SCREENS, VIEWPORTS, measure, prepare, settle, shotPath, type Theme } from './helpers';

// Populated state of every screen. Narrow the matrix with env vars, e.g.
//   VP=390x844,1280x800 THEMES=light,dark LANGS=en npx playwright test -c playwright.visual.config.ts capture
const viewports = (process.env.VP ?? Object.keys(VIEWPORTS).join(',')).split(',');
const themes = (process.env.THEMES ?? 'light,dark,hc').split(',') as Theme[];
const langs = (process.env.LANGS ?? 'en,bn').split(',') as ('en' | 'bn')[];
const screens = (process.env.SCREENS ?? Object.keys(SCREENS).join(',')).split(',');
const zoom = Number(process.env.ZOOM ?? '1');

test('capture populated screens', async ({ browser }) => {
  const findings: Record<string, string[]> = {};
  for (const theme of themes) for (const lang of langs) for (const vp of viewports) {
    const [w, h] = VIEWPORTS[vp];
    // Browser zoom is emulated the way browsers do it: a smaller CSS viewport at a higher device scale.
    const context = await browser.newContext({ viewport: { width: Math.round(w / zoom), height: Math.round(h / zoom) }, deviceScaleFactor: zoom });
    const page = await context.newPage();
    const problems: string[] = [];
    await prepare(page, theme, lang, problems);
    for (const screen of screens) {
      await page.goto(`/#${SCREENS[screen]}`);
      await settle(page);
      const state = zoom === 1 ? 'populated' : `populated-zoom${Math.round(zoom * 100)}`;
      await page.screenshot({ path: shotPath(screen, state, vp, theme, lang), fullPage: false });
      const facts = [...await measure(page), ...problems.splice(0)];
      if (facts.length) findings[`${screen} ${state} ${vp} ${theme} ${lang}`] = facts;
    }
    await context.close();
  }
  writeFileSync(join(OUT, `findings-populated-zoom${Math.round(zoom * 100)}.json`), JSON.stringify(findings, null, 1));
});
