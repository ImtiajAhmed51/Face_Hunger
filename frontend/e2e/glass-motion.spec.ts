import { mkdirSync } from 'node:fs';
import { expect, go, test } from './fixtures';

for (const theme of ['light', 'dark'] as const) {
  test(`glass material and motion preferences: ${theme}`, async ({ page, errors }) => {
    void errors;
    mkdirSync('test-results/glass', { recursive: true });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
    await page.setViewportSize({ width: 390, height: 844 });
    await go(page, '/settings');
    const material = await page.locator('.bottom-nav').evaluate(el => {
      const css = getComputedStyle(el);
      return { background: css.backgroundColor, blur: css.backdropFilter, radius: css.borderRadius };
    });
    expect(material.background).toMatch(/^rgba\(/);
    expect(material.blur).toContain('blur(');
    expect(material.radius).toBe('999px');
    await page.screenshot({ path: `test-results/glass/mobile-${theme}.png` });
    await page.setViewportSize({ width: 1440, height: 900 });
    await go(page, '/people');
    await page.screenshot({ path: `test-results/glass/desktop-${theme}.png` });
    const transitions = await page.locator('.nav-link').first().evaluate(el => getComputedStyle(el).transitionDuration);
    expect(transitions.split(',').every(value => parseFloat(value) < .001)).toBeTruthy();
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await go(page, '/people/1');
    await page.getByRole('button', { name: 'Rename', exact: true }).click();
    const dialog = page.locator('dialog[open]');
    await expect(dialog).toBeVisible();
    expect(await dialog.evaluate(el => getComputedStyle(el).animationName)).toBe('glass-reveal');
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.getByRole('button', { name: 'Rename', exact: true }).click();
    expect(parseFloat(await dialog.evaluate(el => getComputedStyle(el).animationDuration))).toBeLessThan(.001);
    await page.screenshot({ path: `test-results/glass/dialog-${theme}.png` });
    await page.keyboard.press('Escape');
  });
}
