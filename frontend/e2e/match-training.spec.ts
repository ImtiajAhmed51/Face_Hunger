import { expect, go, test } from './fixtures';
for (const width of [390, 768, 1440]) {
  test(`local training needs human labels and fits review layout at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await go(page, '/review');
    const panel = page.getByRole('region', { name: 'Local matching training' });
    await expect(panel).toBeVisible();
    await expect(panel).toContainText('Cosine matching');
    const response = page.waitForResponse(r => r.url().endsWith('/api/review/training') && r.request().method() === 'POST');
    await panel.getByRole('button', { name: 'Train matching model', exact: true }).click();
    expect((await (await response).json()).activated).toBe(false);
    await expect(panel.getByRole('status')).toContainText('Need 100 scored human reviews');
    const status = await page.request.get('/api/review/training');
    expect((await status.json()).active).toBe(false);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    await panel.getByText('Training requirements', { exact: true }).click();
    await expect(panel.locator('details')).toHaveAttribute('open', '');
    await panel.getByText('Training requirements', { exact: true }).click();
    const deletion = page.waitForResponse(r => r.url().endsWith('/api/review/training') && r.request().method() === 'DELETE');
    await panel.getByRole('button', { name: 'Reset learned model', exact: true }).click();
    expect((await deletion).ok()).toBe(true);
    await expect(panel.getByRole('status')).toContainText('reviews and embeddings are preserved');
    await expect(panel.getByRole('button', { name: 'Reset learned model', exact: true })).toHaveCount(0);
    await page.screenshot({ path: `/tmp/fh-match-training-${width}.png`, fullPage: false });
  });
}
