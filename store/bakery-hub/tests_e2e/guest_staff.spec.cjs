const { test, expect } = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
for (const width of [390, 1280]) {
  test(`Guest staff isolation ${width}`, async ({page, request, browser}) => {
    const auth = JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR, 'auth.json')));
    const headers = {'X-E2E-Control': auth.control};
    await request.post('/__e2e/guest-register?enabled=1&staff=1', {headers});
    const other = await browser.newContext();
    let second;
    try {
      await page.setViewportSize({width, height:800});
      await page.goto('/login');
      await page.locator('#guest-demo-start').click();
      await page.goto('/staff-shifts/staff/new');
      const name = `架空スタッフ${width}`;
      await page.locator('[name=display_name]').fill(name);
      await page.getByRole('button', {name:'登録する', exact:true}).click();
      await page.goto('/staff-shifts/shifts/new');
      await page.locator('[name=staff_id]').selectOption({label:name});
      await page.locator('[name=status]').selectOption('all_day');
      await page.getByRole('button', {name:'保存する', exact:true}).click();
      await page.goto('/home');
      await expect(page.locator('main')).toContainText(name);
      second = await other.newPage();
      await second.goto('http://127.0.0.1:8765/login');
      await second.locator('#guest-demo-start').click();
      await second.goto('http://127.0.0.1:8765/staff-shifts');
      await expect(second.locator('main')).not.toContainText(name);
      await request.post('/__e2e/guest-register?enabled=1&staff=0', {headers});
      expect((await page.request.get('/staff-shifts')).status()).toBe(403);
      await page.goto('/home');
      await expect(page.locator('.app-sidebar-link-staff-shifts')).toHaveCount(0);
    } finally {
      await page.request.post('/__e2e/end-guest', {headers});
      if (second) await second.request.post('/__e2e/end-guest', {headers});
      await other.close();
      await request.post('/__e2e/guest-register?enabled=0', {headers});
    }
  });
}
