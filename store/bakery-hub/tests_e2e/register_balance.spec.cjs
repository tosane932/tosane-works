const { test, expect } = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { enterCash } = require('./register_helpers.cjs');
const auth = () => JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR, 'auth.json')));
async function catalog(request, count, restore = false) {
    expect((await request.get(`/__e2e/pager-catalog?count=${count}&restore=${restore ? 1 : 0}`,
        {headers:{'X-E2E-Control':auth().control}})).ok()).toBe(true);
}
async function login(page) {
    await page.goto('/login');
    await page.locator('#username').fill(auth().username);
    await page.locator('#password').fill(auth().password);
    await page.locator('#admin-login-form button[type=submit]').click();
    await expect(page).toHaveURL(/\/home/);
}
async function swipe(page, box) {
    const session = await page.context().newCDPSession(page);
    const start = {x:box.x + box.width * .7, y:box.y + 80};
    await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[start]});
    for (let i=1; i<=10; i++) {
        await session.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:start.x - i*13,y:start.y}]});
        await page.waitForTimeout(25);
    }
    await session.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
    await session.detach();
}
test.afterEach(async ({request}) => { await catalog(request,0,true); });
for (const viewport of [{width:1280,height:800},{width:1024,height:768},{width:1440,height:900},{width:390,height:844}]) {
    test(`Chromium ${viewport.width}x${viewport.height}: 配置・連続移動・キーパッド・会計状態保持`, async ({browser,request}, testInfo) => {
        await catalog(request,30);
        const context = await browser.newContext({viewport,hasTouch:true,isMobile:viewport.width<500});
        const page = await context.newPage();
        await login(page); await page.goto('/register/preview');
        const grid = page.locator('#product-grid');
        const checkout = page.locator('.checkout');
        const layout = await page.locator('.product-page').first().locator('.product-tile').evaluateAll(nodes => nodes.map(node => {
            const box=node.getBoundingClientRect();return {x:box.x,y:box.y,w:box.width,h:box.height};
        }));
        if (viewport.width>850) {
            expect(new Set(layout.map(box=>box.y)).size).toBe(2);
            expect(layout.every(box=>Math.abs(box.w-box.h)<2)).toBe(true);
            expect(await page.locator('#complete-checkout').evaluate(node=>node.getBoundingClientRect().bottom)).toBeLessThanOrEqual(viewport.height);
            const left=await grid.boundingBox(), right=await checkout.boundingBox();
            expect(right.x).toBeGreaterThan(left.x+left.width);
            expect(right.width / viewport.width).toBeLessThan(.36);
        }
        expect(await grid.evaluate(node=>getComputedStyle(node).scrollSnapType)).toBe('none');
        await page.locator('.add-one').first().click();
        await enterCash(page,'2000');
        await expect(page.locator('#received-display')).toHaveText('2,000');
        await expect(page.locator('#change-yen')).toHaveText('1,000円');
        await page.locator('[data-cash-key="backspace"]').click();
        await expect(page.locator('#received-display')).toHaveText('200');
        await expect(page.locator('#complete-checkout')).toBeDisabled();
        await page.locator('[data-cash-key="0"]').click();
        await page.locator('[data-cash-key="clear"]').click();
        await expect(page.locator('#received-display')).toHaveText('—');
        for(const key of ['1','0','00']) { await page.locator(`[data-cash-key="${key}"]`).click(); }
        await expect(page.locator('#received-display')).toHaveText('1,000');
        await page.locator('#cash-keypad').focus();
        await page.keyboard.press('Backspace');
        await page.keyboard.press('0');
        await expect(page.locator('#change-yen')).toHaveText('0円');
        const before=await grid.evaluate(node=> {node.scrollLeft=137;return node.scrollLeft;});
        await page.waitForTimeout(200);
        expect(await grid.evaluate(node=>node.scrollLeft)).toBeCloseTo(before,0);
        await page.locator('.product-tile').nth(15).locator('.add-one').click();
        const offset=await grid.evaluate(node=>node.scrollLeft);
        await enterCash(page,'5000');
        expect(await grid.evaluate(node=>node.scrollLeft)).toBeCloseTo(offset,0);
        await expect(page.locator('#total-yen')).toHaveText('2,000円');
        await expect(page.locator('#change-yen')).toHaveText('3,000円');
        await expect(page.locator('.quantity-input').first()).toHaveValue('1');
        await expect(page.locator('.quantity-input').nth(15)).toHaveValue('1');
        if(viewport.width>850) {
            await grid.evaluate(node=>node.scrollLeft=0);
            await grid.scrollIntoViewIfNeeded();
            await swipe(page,await grid.boundingBox());
            await expect.poll(()=>grid.evaluate(node=>node.scrollLeft)).toBeGreaterThan(20);
            await expect(page.locator('#total-yen')).toHaveText('2,000円');
            await page.waitForTimeout(300);
            const stopped=await grid.evaluate(node=>node.scrollLeft);
            expect(Math.abs(stopped / await grid.evaluate(node=>node.clientWidth)-Math.round(stopped / await grid.evaluate(node=>node.clientWidth)))).toBeGreaterThan(.02);
            // All 30 lines: only the list scrolls; payment controls stay visible.
            for(let i=0;i<30;i++) {
                if(i!==0 && i!==15) { await page.locator('.product-tile').nth(i).locator('.add-one').click(); }
            }
            await enterCash(page,'50000');
            await expect(page.locator('#total-yen')).toHaveText('30,000円');
            await expect(page.locator('#change-yen')).toHaveText('20,000円');
            await page.evaluate(()=>window.scrollTo(0,200));
            await expect(page.locator('#complete-checkout')).toBeInViewport();
            await expect(page.locator('#total-yen')).toBeInViewport();
            await expect(page.locator('#received-display')).toBeInViewport();
            await expect(page.locator('#change-yen')).toBeInViewport();
            expect(await page.locator('#complete-checkout').evaluate(node=>node.getBoundingClientRect().bottom)).toBeLessThanOrEqual(viewport.height);
            expect(await page.locator('#cart-lines').evaluate(node=>node.scrollHeight>node.clientHeight)).toBe(true);
        }
        expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
        await page.screenshot({path:testInfo.outputPath(`register-${viewport.width}.png`),fullPage:true});
        await page.locator('#complete-checkout').click();
        await expect(page.locator('#checkout-result')).toContainText('端末に記録済み');
        await expect(page.locator('#received-display')).toHaveText('—');
        await expect(page.locator('#total-yen')).toHaveText('0円');
        await context.close();
    });
}

test('Chromium: 実写真・2行の商品名・デモ完了後の無効化と次の会計', async ({browser}, testInfo) => {
    const context=await browser.newContext({viewport:{width:1280,height:800},hasTouch:true});
    const page=await context.newPage();
    await login(page);await page.goto('/register/demo');
    const images=page.locator('.product-tile img');
    await expect.poll(()=>images.first().evaluate(img=>img.complete && img.naturalWidth>0)).toBe(true);
    expect(await images.first().evaluate(img=>getComputedStyle(img).objectFit)).toBe('contain');
    expect(await page.locator('.product-label').first().evaluate(node=>getComputedStyle(node).webkitLineClamp)).toBe('2');
    await page.locator('.add-one').first().click();
    await enterCash(page,'1000');
    expect(await page.locator('#complete-checkout').evaluate(node=>node.getBoundingClientRect().bottom)).toBeLessThanOrEqual(800);
    await page.screenshot({path:testInfo.outputPath('tablet-photos.png'),fullPage:true});
    await page.locator('#complete-checkout').click();
    await expect(page.locator('#demo-result')).toBeVisible();
    await expect(page.locator('[data-cash-key="1"]')).toBeDisabled();
    await page.locator('#demo-next').click();
    await expect(page.locator('#received-display')).toHaveText('—');
    await expect(page.locator('#total-yen')).toHaveText('0円');
    await expect(page.locator('[data-cash-key="1"]')).toBeEnabled();
    expect(await page.evaluate(()=>indexedDB.databases())).toEqual([]);
    await context.close();
});
