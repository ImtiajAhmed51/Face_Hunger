import { expect, go, test } from './fixtures';
for (const width of [390, 768, 1440]) {
  test(`cleanup filters unreliable fixture faces at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await go(page, '/cleanup');
    const result = await page.request.get('/api/cleanup');
    expect(result.ok()).toBe(true);
    expect((await result.json()).possible_people).toEqual([]);
    await expect(page.locator('.duplicate-card')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth-innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: `/tmp/fh-merge-evidence-${width}.png` });
  });
}
