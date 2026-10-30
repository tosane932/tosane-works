const { test, expect } = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { enterCash } = require('./register_helpers.cjs');
const auth = () => JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR, 'auth.json')));

for (const viewport of [{ width: 390, height: 800 }, { width: 1280, height: 800 }]) {
    test(`Guest: 初期商品→会計保存→自身の売上 ${viewport.width}`, async ({ page, request, browser }) => {
        const control = { 'X-E2E-Control': auth().control };
        expect((await request.post('/__e2e/guest-register?enabled=1', { headers: control })).ok()).toBe(true);
        const adminBefore = await (await request.get('/__e2e/state', { headers: control })).json();
        const other = await browser.newContext();
        try {
            await page.setViewportSize(viewport);
            await page.goto('/login');
            await page.locator('#guest-demo-start').click();
            await expect(page).toHaveURL(/\/home/);
            await page.goto('/products');
            await expect(page.getByRole('link', { name: '＋ 新しい商品を登録' })).toBeVisible();
            await page.goto('/register/preview');
            await expect(page.locator('.register-mode-note')).toHaveText('ゲスト体験');
            const config = await page.locator('#register-config').evaluate(el => JSON.parse(el.textContent));
            expect(config.products.length).toBeGreaterThan(0);
            expect(config.offlineMode).toBe(false);
            await expect(page.locator('#guest-sales-analysis-link')).toBeHidden();
            await page.locator('.add-one').first().click();
            await enterCash(page, '100000');
            await page.locator('#complete-checkout').click();
            await expect.poll(async () => (await page.request.get('/api/dashboard-data')).json().then(d => d.today_sales)).toBe(config.products[0].price);
            await expect(page.locator('#guest-sales-analysis-link')).toBeVisible();
            await expect(page.locator('#checkout-result-text')).toContainText('売上に反映');
            await page.locator('#guest-sales-analysis-link').click();
            await expect(page).toHaveURL(/\/dashboard(?:\?|$)/);
            expect(await (await request.get('/__e2e/state', { headers: control })).json()).toEqual(adminBefore);
            const second = await other.newPage();
            await second.goto('http://127.0.0.1:8765/login');
            await second.locator('#guest-demo-start').click();
            await expect(second).toHaveURL(/\/home/);
            expect((await (await second.request.get('http://127.0.0.1:8765/api/dashboard-data')).json()).today_sales).toBe(0);
        } finally {
            await other.close();
            await request.post('/__e2e/guest-register?enabled=0', { headers: control });
        }
    });
}
