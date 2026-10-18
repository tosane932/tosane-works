const { test, expect } = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const auth = () => JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR, 'auth.json')));
async function catalog(request, count, restore = false) {
    expect((await request.get(`/__e2e/pager-catalog?count=${count}&restore=${restore ? 1 : 0}`,
        { headers: { 'X-E2E-Control': auth().control } })).ok()).toBe(true);
}
async function login(page) {
    await page.goto('/login');
    await page.locator('#username').fill(auth().username);
    await page.locator('#password').fill(auth().password);
    await page.locator('#admin-login-form button[type=submit]').click();
    await expect(page).toHaveURL(/\/home/);
}
async function drag(page, from, to) {
    const session = await page.context().newCDPSession(page);
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [from] });
    for (let step = 1; step <= 12; step++) {
        await session.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{
            x: from.x + (to.x - from.x) * step / 12,
            y: from.y + (to.y - from.y) * step / 12,
        }] });
        await page.evaluate(() => new Promise(requestAnimationFrame));
    }
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await session.detach();
}
test.afterEach(async ({ request }) => { await catalog(request, 0, true); });

for (const [count, pages] of [[0,0],[8,1],[12,1],[13,2],[18,2],[24,2],[30,3],[50,5]]) {
    test(`Chromium: ${count}商品のページ数と最大12商品・PC代替操作`, async ({ page, request }) => {
        await catalog(request, count);
        await login(page);
        await page.goto('/register/preview');
        await expect(page.locator('.product-tile')).toHaveCount(count);
        await expect(page.locator('.product-page')).toHaveCount(pages);
        await expect(page.locator('#products-position')).toHaveText(pages ? `1 / ${pages}` : '0 / 0');
        const sizes = await page.locator('.product-page').evaluateAll(nodes => nodes.map(node => node.children.length));
        expect(sizes.every(n => n <= 12)).toBe(true);
        if (pages > 1) {
            await page.locator('.product-page').first().locator('.add-one').first().click();
            await page.locator('#received-yen').fill('5000');
            await page.locator('#products-next').click();
            await expect(page.locator('#products-position')).toHaveText(`2 / ${pages}`);
            await page.locator('.product-page').nth(1).locator('.add-one').first().click();
            await expect(page.locator('#total-yen')).toHaveText('2,000円');
            await expect(page.locator('#change-yen')).toHaveText('3,000円');
            await page.locator('#product-grid').focus();
            await page.keyboard.press('ArrowLeft');
            await expect(page.locator('#products-position')).toHaveText(`1 / ${pages}`);
            await expect(page.locator('.product-page').first().locator('.quantity-input').first()).toHaveValue('1');
            await expect(page.locator('#received-yen')).toHaveValue('5000');
            await expect(page.locator('#total-yen')).toHaveText('2,000円');
        }
    });
}
for (const [device, viewport] of [['smartphone', {width:390,height:844}], ['tablet', {width:1024,height:768}]]) {
    test(`Chromium ${device}: 実タッチ横スワイプ・縦スクロール・誤タップ防止`, async ({ browser, request }) => {
        await catalog(request, 30);
        const context = await browser.newContext({ viewport, hasTouch:true, isMobile:device === 'smartphone' });
        const page = await context.newPage();
        await login(page); await page.goto('/register/preview');
        const grid = page.locator('#product-grid');
        await grid.scrollIntoViewIfNeeded();
        const tile = page.locator('.product-page').first().locator('.add-one').first();
        await tile.scrollIntoViewIfNeeded();
        const box = await grid.boundingBox();
        const first = await tile.boundingBox();
        const y = Math.max(60, Math.min(viewport.height - 90, first.y + first.height / 2));
        await drag(page, { x:box.x + box.width * .85,y }, {x:box.x + box.width * .15,y});
        await expect(page.locator('#products-position')).toHaveText('2 / 3');
        await expect(page.locator('#total-yen')).toHaveText('0円');
        await drag(page, { x:box.x + box.width * .15,y }, {x:box.x + box.width * .85,y});
        await expect(page.locator('#products-position')).toHaveText('1 / 3');
        await expect(page.locator('#total-yen')).toHaveText('0円');
        await tile.click();
        await expect(page.locator('#total-yen')).toHaveText('1,000円');
        const before = await page.evaluate(() => window.scrollY);
        await drag(page, {x:box.x + box.width * .5,y:Math.min(viewport.height - 100,y + 150)},
                        {x:box.x + box.width * .5,y:80});
        await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(before);
        await expect(page.locator('#total-yen')).toHaveText('1,000円');
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
        await context.close();
    });
}

test('Chromium: 非保存デモでも横ページを使い、DB・IndexedDBへ会計を保存しない', async ({ page, request }) => {
    await login(page);
    await page.goto('/register/demo');
    await expect(page.locator('.product-page')).toHaveCount(2);
    await page.locator('.add-one').first().click();
    await page.locator('#products-next').click();
    await expect(page.locator('#products-position')).toHaveText('2 / 2');
    await page.locator('.product-page').nth(1).locator('.add-one').first().click();
    await page.locator('#received-yen').fill('10000');
    const state = () => request.get('/__e2e/state', {headers:{'X-E2E-Control':auth().control}}).then(r => r.json());
    const before = await state();
    await page.locator('#complete-checkout').click();
    await expect(page.locator('#demo-result')).toBeVisible();
    expect(await state()).toEqual(before);
    expect(await page.evaluate(() => indexedDB.databases())).toEqual([]);
});

test('Chromium: 写真中心の登録・終売と再開・暗号化商品版のオフライン写真と会計', async ({ browser, request }) => {
    await catalog(request, 13);
    const context = await browser.newContext({ viewport:{width:1024,height:768},hasTouch:true });
    const page = await context.newPage();
    await login(page); await page.goto('/products');
    await page.getByRole('link',{name:'＋ 新しい商品を登録'}).click();
    await page.locator('input[name=photo]').setInputFiles(path.join(__dirname,'../static/product_images/croissant.png'));
    await expect(page.locator('#product-photo-preview')).toBeVisible();
    await page.locator('input[name=name]').fill('写真登録の食事パン');
    await page.locator('input[name=price_yen]').fill('850');
    await page.locator('select[name=bread_kind]').selectOption('loaf');
    await page.locator('select[name=product_class]').selectOption('seasonal');
    await page.locator('#product-editor button[type=submit]').click();
    await expect(page.locator('#product-confirm-summary')).toContainText('850円');
    await page.locator('#product-confirm-save').click();
    await expect(page).toHaveURL(/\/products/);
    let card = page.locator('.register-product-card').filter({has:page.getByRole('heading',{name:'写真登録の食事パン',exact:true})});
    const productId = await card.getAttribute('id');
    await expect(card.locator('img')).toBeVisible();
    await expect(card).toContainText('食パン・食事パン ／ 季節もの');
    await card.getByRole('button',{name:'終売にする（商品を保存）'}).click();
    card = page.locator(`#${productId}`);
    await expect(card).toContainText('終売');
    await card.getByRole('link',{name:'価格を確認・販売中に戻す'}).click();
    await expect(page.locator('input[name=price_yen]')).toHaveValue('850');
    await page.locator('form[action$="/status"] input[type=checkbox]').check();
    await page.getByRole('button',{name:'販売中に戻す',exact:true}).click();
    await expect(page.locator(`#${productId}`)).toContainText('販売中');
    await page.goto('/register/preview');
    await page.locator('#offline-setup-pin').fill('e2e-photo-offline-pin');
    await page.locator('#offline-setup button[type=submit]').click();
    await expect(page.locator('#offline-setup-state')).toContainText('端末を準備しました');
    await page.locator('#offline-setup-state a').click();
    await page.locator('#offline-pin').fill('e2e-photo-offline-pin');
    await page.locator('#offline-unlock button').click();
    await expect(page.locator('#offline-unlock-state')).toContainText('端末内レジを開きました');
    await context.setOffline(true);
    await page.locator('#products-next').click();
    const photoTile = page.locator('.product-tile').filter({has:page.getByRole('button',{name:'写真登録の食事パンを1個追加',exact:true})});
    await expect(photoTile.locator('img')).toBeVisible();
    expect(await photoTile.locator('img').evaluate(img => img.src.startsWith('data:image/webp;base64,') && img.naturalWidth>0)).toBe(true);
    await photoTile.locator('.add-one').click();
    await page.locator('#products-previous').click();
    await page.locator('.product-page').first().locator('.add-one').first().click();
    await page.locator('#received-yen').fill('2000');
    await expect(page.locator('#total-yen')).toHaveText('1,850円');
    await expect(page.locator('#change-yen')).toHaveText('150円');
    await page.locator('#complete-checkout').click();
    await expect(page.locator('#checkout-result')).toContainText('端末に記録済み');
    await context.setOffline(false);
    await expect(page.locator('#sync-state')).toContainText('未同期 0件',{timeout:30000});
    await context.close();
});
