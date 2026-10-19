const {test,expect}=require('playwright/test');
const fs=require('node:fs');
const path=require('node:path');
const {enterCash}=require('./register_helpers.cjs');
const auth=()=>JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR,'auth.json')));
async function login(page) {
    await page.goto('/login');await page.locator('#username').fill(auth().username);
    await page.locator('#password').fill(auth().password);
    await page.locator('#admin-login-form button[type=submit]').click();
    await expect(page).toHaveURL(/\/home/);
}
async function measurement(page) {
    return page.evaluate(()=>{
        const box=s=>{const b=document.querySelector(s).getBoundingClientRect();return {top:b.top,bottom:b.bottom,w:b.width,h:b.height};};
        return {viewport:[innerWidth,innerHeight,visualViewport.height],fit:document.body.classList.contains('register-fit'),
            overflow:document.documentElement.scrollHeight-innerHeight,header:box('.register-header'),catalog:box('.catalog'),
            tile:box('.product-tile'),photo:box('.product-tile img:not([hidden]),.product-tile .no-image:not([hidden])'),
            key:box('[data-cash-key="7"]'),complete:box('#complete-checkout'),cart:box('#cart-lines'),
            payment:box('#checkout-payment'),total:box('.total-row')};
    });
}
async function paymentVisible(page,height) {
    for(const selector of ['#total-yen','#received-display','#cash-keypad','#change-yen','#complete-checkout']) {
        await expect(page.locator(selector)).toBeInViewport({ratio:1});
        expect(await page.locator(selector).evaluate((el,h)=>{
            const b=el.getBoundingClientRect();return b.bottom<=h && el.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2));
        },height)).toBe(true);
    }
}
for(const viewport of [{width:1024,height:500},{width:1024,height:533},{width:1024,height:540},{width:1280,height:552},{width:1280,height:600}]) {
    for(const route of ['/register/demo','/register/preview']) {
        test(`${route} ${viewport.width}x${viewport.height}: trim decoration, preserve photos and payment`,async({browser},info)=>{
            const context=await browser.newContext({viewport,hasTouch:true});const page=await context.newPage();
            await login(page);await page.goto(route);await expect(page.locator('.product-tile').first()).toBeVisible();
            await page.waitForTimeout(150);
            const empty=await measurement(page);console.log('TRIM empty',JSON.stringify({route,...empty}));
            await info.attach('empty.json',{body:JSON.stringify(empty,null,2),contentType:'application/json'});
            expect(empty.fit).toBe(true);expect(empty.overflow).toBe(0);
            expect(empty.header.h).toBeLessThan(44);expect(empty.catalog.top).toBeLessThanOrEqual(60);
            expect(empty.cart.h).toBe(0);
            for(const selector of ['#register-back','#products-previous','#products-next']) {
                const b=await page.locator(selector).boundingBox();
                expect(b.height).toBeGreaterThanOrEqual(44);expect(b.y).toBeGreaterThanOrEqual(0);
                expect(b.y+b.height).toBeLessThanOrEqual(empty.tile.top);
            }
            await expect(page.locator('.preview-note')).toBeHidden();
            await expect(page.locator('#register-layout-notice')).toBeHidden();
            if(route.endsWith('demo')) await expect(page.locator('#sync-state')).toContainText('練習用・未保存');
            else await expect(page.locator('.register-mode-note')).toBeVisible();
            await page.locator('.add-one').first().tap();
            await page.locator('.remove-one').first().tap();await expect(page.locator('.quantity-input').first()).toHaveValue('0');
            await page.locator('.add-one').first().tap();await enterCash(page,'10000');
            const total=Number((await page.locator('#total-yen').textContent()).replace(/\D/g,''));
            await expect(page.locator('#change-yen')).toHaveText(`${(10000-total).toLocaleString('ja-JP')}円`);
            const single=await measurement(page);console.log('TRIM single',JSON.stringify({route,...single}));
            await info.attach('single.json',{body:JSON.stringify(single,null,2),contentType:'application/json'});
            expect(single.cart.h).toBeLessThanOrEqual(44);expect(single.total.top-single.cart.bottom).toBeLessThanOrEqual(6);
            expect(single.tile.h).toBeGreaterThanOrEqual(168);expect(single.photo.h/single.tile.h).toBeGreaterThan(.5);
            expect(single.key.h).toBeGreaterThanOrEqual(52);expect(single.complete.h).toBeGreaterThanOrEqual(56);
            await paymentVisible(page,viewport.height);
            expect(await page.locator('.product-page').first().evaluate(el=>getComputedStyle(el).gridTemplateRows.split(' ').length)).toBe(2);
            await expect(page.locator('.increase-one')).toHaveCount(0);
            expect(await page.locator('.remove-one').first().evaluate(el=>el.getBoundingClientRect().height)).toBe(48);
            if(route.endsWith('demo')) {
                for(let i=1;i<10;i++) await page.locator('.add-one').nth(i).click();
                expect(await page.locator('#cart-lines').evaluate(el=>el.scrollHeight>el.clientHeight)).toBe(true);
                await paymentVisible(page,viewport.height);
                expect(await page.evaluate(()=>document.documentElement.scrollHeight-innerHeight)).toBe(0);
            }
            await page.screenshot({path:info.outputPath('trim.png')});await context.close();
        });
    }
}
