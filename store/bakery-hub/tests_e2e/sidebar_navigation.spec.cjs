// Navigation only: disposable DB, fake RSS, no external analysis or production flags.
const {test, expect} = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const auth = () => JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR, 'auth.json')));

test.beforeEach(async ({context}) => {
    await context.route(/^https?:\/\//, route =>
        new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
});

async function login(page, guest = false) {
    await page.goto('/login');
    if (guest) {
        await page.locator('#guest-demo-start').click();
    } else {
        await page.locator('#username').fill(auth().username);
        await page.locator('#password').fill(auth().password);
        await page.locator('#admin-login-form button[type=submit]').click();
    }
    await expect(page).toHaveURL(/\/home/);
}
async function menu(page, width) {
    if (width <= 900) await page.getByRole('button', {name:'主要メニューを開く', exact:true}).click();
    await expect.poll(() => page.locator('#app-sidebar').evaluate(node =>
        Math.round(node.getBoundingClientRect().left))).toBe(0);
}

for (const width of [360, 390, 800, 1280]) {
    test(`Chromium ${width}px: nine entries, full-row click, collapsed admin controls and keyboard`, async ({page}) => {
        await page.setViewportSize({width, height:800});
        await login(page);
        await menu(page, width);
        const sidebar = page.locator('#app-sidebar');
        const nav = sidebar.locator('nav');
        await expect(nav.locator(':scope > a[data-page], .app-sidebar-home-row [data-page]')).toHaveCount(9);
        const measures = await nav.locator(':scope > a[data-page], .app-sidebar-home-row [data-page]').evaluateAll(nodes =>
            nodes.map(n => ({height:n.getBoundingClientRect().height, overflow:n.scrollWidth > n.clientWidth})));
        expect(measures.every(m => m.height >= 44 && !m.overflow)).toBe(true);
        const details = sidebar.locator('#app-register-management');
        await expect(details).not.toHaveAttribute('open', '');
        const summary = details.locator('summary');
        await summary.focus();
        await page.keyboard.press('Space');
        await expect(summary).toHaveAttribute('aria-expanded', 'true');
        await expect(details.getByRole('link', {name:'レジ操作デモ（未保存）'})).toBeVisible();
        await page.keyboard.press('Space');
        await expect(summary).toHaveAttribute('aria-expanded', 'false');
        // Tab wraps past only visible controls; hidden submenu links never receive focus.
        if (width <= 900) {
            await sidebar.locator('.app-sidebar-logout-button').focus();
            await page.keyboard.press('Tab');
            await expect(sidebar.locator('.app-sidebar-brand')).toBeFocused();
            await page.keyboard.press('Shift+Tab');
            await expect(sidebar.locator('.app-sidebar-logout-button')).toBeFocused();
        }
        const news = sidebar.locator('[data-page=news]');
        await expect(news).toHaveCSS('background-color', 'rgb(72, 115, 156)');
        await news.scrollIntoViewIfNeeded();
        const size = await news.boundingBox();
        await news.click({position:{x:size.width-8, y:size.height/2}});
        await expect(page).toHaveURL(/\/bakery-news/);
        await expect(page.locator('main h1')).toHaveText('ニュース・AI分析');
        await expect(page.locator('#puoppo form')).toHaveCount(0);
        await menu(page, width);
        await expect(sidebar.locator('[data-page=news]')).toHaveAttribute('aria-current', 'page');
        await page.screenshot({path:test.info().outputPath('sidebar.png')});
        await sidebar.locator('[data-page=product-catalog]').click();
        await expect(page).toHaveURL(/\/products/);
        await expect(page.getByRole('link', {name:'＋ 新しい商品を登録', exact:true})).toBeVisible();
        await menu(page, width);
        await sidebar.locator('[data-page=shop-tools]').click();
        await menu(page, width);
        await expect(sidebar.locator('#app-tools-submenu')).toBeVisible();
        await sidebar.locator('#app-tools-submenu').getByRole('link', {name:'タスク管理'}).click();
        await expect(page).toHaveURL(/\/shop-tools\/tasks/);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        if (width <= 900) {
            await menu(page, width);
            await page.keyboard.press('Escape');
            await expect(page.getByRole('button', {name:'主要メニューを開く', exact:true})).toBeFocused();
        }
    });
}

test('Guest retains legacy product registration and cannot access Admin controls', async ({page}) => {
    await page.setViewportSize({width:390, height:844});
    await login(page, true);
    await menu(page, 390);
    const sidebar = page.locator('#app-sidebar');
    await expect(sidebar.locator('[data-page=staff-shifts], [data-page=prep], #app-register-management')).toHaveCount(0);
    await expect(sidebar.locator('.app-sidebar-link-sales')).toHaveText('日次売上入力');
    await sidebar.locator('[data-page=product-catalog]').click();
    await page.locator('main a[href="/"]').click();
    await expect(page).toHaveURL(/\/$/);
    await menu(page, 390);
    await expect(sidebar.locator('[data-page=product-catalog]')).toHaveAttribute('aria-current', 'page');
    await sidebar.locator('[data-page=news]').click();
    await expect(page.locator('#puoppo form')).toHaveCount(0);
    await expect(page.locator('#puoppo')).toContainText('ゲストデモでは');
});
