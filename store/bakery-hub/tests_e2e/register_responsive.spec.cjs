const {test,expect}=require('playwright/test');
const fs=require('node:fs');
const path=require('node:path');
const {enterCash}=require('./register_helpers.cjs');
const auth=()=>JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR,'auth.json')));
async function catalog(request,count,restore=false) {
    expect((await request.get(`/__e2e/pager-catalog?count=${count}&restore=${restore?1:0}`,
        {headers:{'X-E2E-Control':auth().control}})).ok()).toBe(true);
}
async function login(page) {
    await page.goto('/login');
    await page.locator('#username').fill(auth().username);
    await page.locator('#password').fill(auth().password);
    await page.locator('#admin-login-form button[type=submit]').click();
    await expect(page).toHaveURL(/\/home/);
}
async function visibleControl(locator,height) {
    await expect(locator).toBeInViewport({ratio:1});
    expect(await locator.evaluate(el=>{
        const b=el.getBoundingClientRect();
        const hit=document.elementFromPoint(b.x+b.width/2,b.y+b.height/2);
        return el.contains(hit);
    })).toBe(true);
    const box=await locator.boundingBox();
    expect(box.bottom??box.y+box.height).toBeLessThanOrEqual(height+1);
}
test.afterEach(async({request})=>catalog(request,0,true));

for(const width of [360,375,390,430]) {
    test(`phone ${width}: all 25 products in two vertical columns and direct payment navigation`,async({browser,request},info)=>{
        await catalog(request,25);
        const context=await browser.newContext({viewport:{width,height:800},isMobile:true,hasTouch:true});
        const page=await context.newPage();
        await login(page);await page.goto('/register/preview');
        await expect(page.locator('.product-tile')).toHaveCount(25);
        await expect(page.locator('.product-page,.product-placeholder')).toHaveCount(0);
        await expect(page.locator('.product-page-controls')).not.toBeVisible();
        const layout=await page.locator('.product-tile').evaluateAll(nodes=>nodes.slice(0,3).map(el=>{
            const b=el.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height};
        }));
        expect(Math.abs(layout[0].y-layout[1].y)).toBeLessThan(2);
        expect(layout[1].x).toBeGreaterThan(layout[0].x+layout[0].w);
        expect(layout[2].y).toBeGreaterThan(layout[0].y+layout[0].h);
        expect(await page.locator('#product-grid').evaluate(el=>el.scrollWidth<=el.clientWidth+1)).toBe(true);
        expect(await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight)).toBe(true);
        const first=page.locator('.product-tile').first();
        await first.locator('.add-one').tap();
        await expect(page.locator('#jump-checkout')).toContainText('1,000円');
        await first.locator('.remove-one').tap();
        await expect(first.locator('.quantity-input')).toHaveValue('0');
        await first.locator('.add-one').tap();
        await page.locator('.product-tile').last().scrollIntoViewIfNeeded();
        await page.locator('.product-tile').last().locator('.add-one').tap();
        await expect(page.locator('#jump-checkout')).toContainText('2,000円');
        await page.evaluate(()=>window.scrollBy(0,-450));
        await expect(page.locator('#jump-checkout')).toBeVisible();
        const productY=await page.evaluate(()=>scrollY);
        await page.locator('#jump-checkout').click();
        for(const selector of ['#total-yen','#received-display','#cash-keypad','#change-yen','#complete-checkout']) {
            await visibleControl(page.locator(selector),800);
        }
        await expect(page.locator('#jump-checkout')).not.toBeVisible();
        await enterCash(page,'5000');
        await expect(page.locator('#change-yen')).toHaveText('3,000円');
        await page.screenshot({path:info.outputPath(`phone-payment-${width}.png`)});
        await page.locator('#return-products').click();
        await expect.poll(()=>page.evaluate(()=>scrollY)).toBeCloseTo(productY,0);
        await expect(page.locator('#received-display')).toHaveText('5,000');
        await expect(first.locator('.quantity-input')).toHaveValue('1');
        await expect(page.locator('.product-tile').last().locator('.quantity-input')).toHaveValue('1');
        expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
        await context.close();
    });
}

test('tablet: 0/1/6/12/13/18/24/25 products, only final group padded and one-screen payment',async({browser,request},info)=>{
    for(const count of [0,1,6,12,13,18,24,25]) {
        await catalog(request,count);
        const context=await browser.newContext({viewport:{width:1024,height:768},hasTouch:true});
        const page=await context.newPage();await login(page);await page.goto('/register/preview');
        await expect(page.locator('.product-tile')).toHaveCount(count);
        await expect(page.locator('.product-page')).toHaveCount(Math.ceil(count/12));
        const missing=count?(2-count%2)%2:0;
        await expect(page.locator('.product-placeholder')).toHaveCount(missing);
        if(missing) {
            const dummy=page.locator('.product-placeholder').last();
            expect(await dummy.getAttribute('aria-hidden')).toBe('true');
            await expect(dummy.locator('button,input,a,[tabindex],[data-product-id]')).toHaveCount(0);
            expect(await dummy.getAttribute('data-product-id')).toBeNull();
            await dummy.scrollIntoViewIfNeeded();await dummy.click();
            await expect(page.locator('#total-yen')).toHaveText('0円');
            expect(await dummy.evaluate(el=>el.getBoundingClientRect().width)).toBeCloseTo(
                await page.locator('.product-tile').first().evaluate(el=>el.getBoundingClientRect().width),0);
        }
        if(count) {
            const boxes=await page.locator('.product-page').first().evaluate(el=>Array.from(el.children).map(n=>{
                const b=n.getBoundingClientRect();return {y:b.y,w:b.width,h:b.height};
            }));
            expect(new Set(boxes.map(b=>b.y)).size).toBe(Math.min(2,count+missing));
            expect(boxes.every(b=>Math.abs(b.w-b.h)<2)).toBe(true);
        } else await expect(page.locator('#product-grid')).toContainText('販売中の商品はありません');
        await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollHeight-innerHeight)).toBeLessThanOrEqual(1);
        await page.evaluate(()=>window.scrollTo(0,1000));
        expect(await page.evaluate(()=>scrollY)).toBe(0);
        for(const selector of ['#total-yen','#received-display','#cash-keypad','#change-yen','#complete-checkout']) {
            await visibleControl(page.locator(selector),768);
        }
        await page.screenshot({path:info.outputPath(`tablet-${count}.png`)});
        await context.close();
    }
});

test('rotation retains DOM identity, cart and cash; low height and large text keep payment reachable',async({page,request})=>{
    await catalog(request,25);await page.setViewportSize({width:390,height:844});
    await login(page);await page.goto('/register/preview');
    await page.locator('.add-one').first().click();await page.locator('#jump-checkout').click();
    await enterCash(page,'5000');
    await page.evaluate(()=>{window.originalTile=document.querySelector('.product-tile');});
    for(const viewport of [{width:1024,height:768},{width:390,height:844},{width:1280,height:720}]) {
        await page.setViewportSize(viewport);
        await expect(page.locator('.quantity-input').first()).toHaveValue('1');
        await expect(page.locator('#received-display')).toHaveText('5,000');
        await expect(page.locator('#total-yen')).toHaveText('1,000円');
        expect(await page.evaluate(()=>window.originalTile===document.querySelector('.product-tile'))).toBe(true);
        if(viewport.width>850) {
            await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollHeight-innerHeight)).toBeLessThanOrEqual(1);
            for(const selector of ['#total-yen','#received-display','#cash-keypad','#change-yen','#complete-checkout']) {
                await visibleControl(page.locator(selector),viewport.height);
            }
        } else await expect(page.locator('.product-page,.product-placeholder')).toHaveCount(0);
    }
    await page.setViewportSize({width:1024,height:500});
    await expect(page.locator('#register-layout-notice')).toBeVisible();
    await page.locator('#complete-checkout').scrollIntoViewIfNeeded();
    await expect(page.locator('#complete-checkout')).toBeInViewport({ratio:1});
    await page.setViewportSize({width:1024,height:768});
    await page.evaluate(()=>document.documentElement.style.fontSize='24px');
    await page.locator('#complete-checkout').scrollIntoViewIfNeeded();
    await expect(page.locator('#complete-checkout')).toBeInViewport({ratio:1});
    await expect(page.locator('#received-display')).toHaveText('5,000');
    await expect(page.locator('#total-yen')).toHaveText('1,000円');
    await page.evaluate(()=>{
        document.documentElement.style.fontSize='16px';
        const warning=document.querySelector('#register-message');
        warning.hidden=false;warning.textContent='端末に保存できません。入力中の会計を保持しています。'.repeat(60);
    });
    await expect(page.locator('#register-layout-notice')).toBeVisible();
    await expect(page.locator('#register-message')).toBeVisible();
    await page.locator('#complete-checkout').scrollIntoViewIfNeeded();
    await expect(page.locator('#complete-checkout')).toBeInViewport({ratio:1});
    await expect(page.locator('#total-yen')).toHaveText('1,000円');
});
