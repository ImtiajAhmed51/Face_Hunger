import { test as base, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

/** Every test fails on console errors or failed requests, and can assert WCAG 2.2 AA with axe. */
export const test = base.extend<{ errors: string[]; axe: (page: Page, label: string) => Promise<void> }>({
  errors: async ({ page }, use) => {
    const errors: string[] = [];
    page.on('console', (msg) => { if (msg.type() === 'error') errors.push(msg.text()); });
    page.on('pageerror', (err) => errors.push(err.message));
    page.on('requestfailed', (req) => {
      const reason = req.failure()?.errorText ?? '';
      // Aborted image/stream requests during navigation are expected, not failures.
      if (!reason.includes('ERR_ABORTED')) errors.push(`${req.url()} ${reason}`);
    });
    await use(errors);
    expect(errors, 'console errors').toEqual([]);
  },
  axe: async ({}, use) => {
    await use(async (page: Page, label: string) => {
      const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']).analyze();
      const summary = results.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.length}x ${v.nodes[0]?.target.join(' ')}`);
      expect(summary, `axe violations on ${label}`).toEqual([]);
    });
  },
});

export { expect };

export async function go(page: Page, hash: string) {
  await page.goto(`/#${hash}`);
  await page.waitForLoadState('networkidle').catch(() => {});
}
