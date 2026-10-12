import { expect, go, test } from './fixtures';
for (const width of [390, 768, 1440]) {
  test(`duplicate type filter switches groups and clears selection at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    // Label a video group in the disposable response; no library records are modified.
    await page.route('**/api/duplicates?*', async route => {
      const response = await route.fetch();
      const data = await response.json();
      const photo = data.groups.find((g: { items: { kind: string }[] }) => g.items.length >= 2 && g.items.every(i => i.kind === 'photo'));
      expect(photo).toBeTruthy();
      const video = { ...photo, key: 'fixture-video-group', suggestion: undefined,
        items: photo.items.map((i: { name: string }) => ({ ...i, kind: 'video', name: `${i.name}.mp4` })) };
      await route.fulfill({ response, json: { ...data, groups: [photo, video] } });
    });
    await go(page, '/duplicates');
    const tabs = page.getByRole('group', { name: 'Duplicate media type', exact: true });
    await expect(page.locator('.dino-group')).toHaveCount(2);
    await tabs.getByRole('button', { name: 'Videos', exact: true }).click();
    await expect(page.locator('.dino-group')).toHaveCount(1);
    await expect(page.locator('.dino-item').first()).toContainText('.mp4');
    await page.locator('.dino-item input[type=checkbox]').first().check();
    await expect(page.locator('.dino-item.selected')).toHaveCount(1);
    await tabs.getByRole('button', { name: 'Photos', exact: true }).click();
    await expect(page.locator('.dino-group')).toHaveCount(1);
    await expect(page.locator('.dino-item.selected')).toHaveCount(0);
    await expect(tabs.getByRole('button', { name: 'Photos', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await tabs.getByRole('button', { name: 'All', exact: true }).click();
    await expect(page.locator('.dino-group')).toHaveCount(2);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  });
}
