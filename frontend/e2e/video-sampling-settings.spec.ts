import { expect, go, test } from './fixtures';
for (const width of [390,768,1440]) {
  test(`video sampling preferences persist at ${width}px`,async ({page})=>{
    const previous=await (await page.request.get('/api/settings?storage=false')).json();
    try {
      await page.setViewportSize({width,height:900});
      await go(page,'/settings');
      await page.getByLabel('Video sampling interval',{exact:true}).fill('6');
      await page.getByLabel('Adaptive video sampling',{exact:true}).uncheck();
      const saved=page.waitForResponse(r=>r.url().endsWith('/api/settings')&&r.request().method()==='PATCH');
      await page.getByRole('button',{name:'Save changes',exact:true}).click();
      expect((await saved).ok()).toBe(true);
      await page.reload();
      await expect(page.getByLabel('Video sampling interval',{exact:true})).toHaveValue('6');
      await expect(page.getByLabel('Adaptive video sampling',{exact:true})).not.toBeChecked();
      expect(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth)).toBeLessThanOrEqual(1);
    } finally {
      await page.request.patch('/api/settings',{headers:{'X-LFS-Request':'1'},data:{video_interval:previous.video_interval,adaptive_video:previous.adaptive_video}});
    }
  });
}
