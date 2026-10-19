const {test,expect}=require('playwright/test');
const fs=require('node:fs');
const path=require('node:path');
async function login(page) {
    const auth=JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR,'auth.json')));
    await page.goto('/login');
    await page.locator('#username').fill(auth.username);
    await page.locator('#password').fill(auth.password);
    await page.locator('#admin-login-form button[type=submit]').click();
    await expect(page).toHaveURL(/\/home/);
}
for(const viewport of [{width:1024,height:768},{width:1280,height:800},{width:390,height:844},{width:320,height:740}]) {
    test(`Chromium ${viewport.width}: 大きい写真と追加領域・独立した減算・数量入力`,async({browser},testInfo)=>{
        const context=await browser.newContext({viewport,hasTouch:true,isMobile:viewport.width<500});
        const page=await context.newPage();
        await login(page);await page.goto('/register/demo');
        const tile=page.locator('.product-tile').first();
        await expect(tile.locator('img')).toBeVisible();
        const metrics=await tile.evaluate(node=>{
            const box=node.getBoundingClientRect(),img=node.querySelector('img').getBoundingClientRect();
            const minus=node.querySelector('.remove-one').getBoundingClientRect();
            return {width:box.width,height:box.height,imageWidth:img.width,imageHeight:img.height,minusWidth:minus.width,minusHeight:minus.height};
        });
        console.log(`tile metrics ${viewport.width}: ${JSON.stringify(metrics)}`);
        await expect(page.locator('.increase-one')).toHaveCount(0);
        expect(metrics.minusWidth).toBeGreaterThanOrEqual(44);
        expect(metrics.minusHeight).toBeGreaterThanOrEqual(44);
        if(viewport.width>850) {
            expect(Math.abs(metrics.width-metrics.height)).toBeLessThan(2);
            expect(metrics.imageHeight/metrics.height).toBeGreaterThan(.43);
        } else {
            expect(metrics.height).toBeGreaterThanOrEqual(222);
            expect(metrics.imageHeight/metrics.height).toBeGreaterThan(.35);
            expect(metrics.imageWidth/metrics.width).toBeGreaterThan(.38);
        }
        await page.screenshot({path:testInfo.outputPath(`tile-${viewport.width}.png`),fullPage:true});
        const quantity=tile.locator('.quantity-input');
        let value=0;
        for(const part of ['img','.product-label','.product-price']) {
            await tile.locator(part).tap();
            await expect(quantity).toHaveValue(String(++value));
        }
        await tile.tap({position:{x:3,y:3}}); // Blank padding is owned by the add button.
        await expect(quantity).toHaveValue(String(++value));
        for(let i=0;i<5;i++) { await tile.locator('.add-one').tap(); }
        value+=5;
        await expect(quantity).toHaveValue(String(value));
        const minus=tile.locator('.remove-one');
        expect(await minus.evaluate(node=>parseFloat(getComputedStyle(node,'::before').width))).toBe(28);
        await minus.tap({position:{x:2,y:2}}); // Outside the 28px visual, inside its 48px hit area.
        await expect(quantity).toHaveValue(String(--value));
        await minus.locator('span').tap();
        await expect(quantity).toHaveValue(String(--value));
        await minus.focus(); await page.keyboard.press('Enter');
        await expect(quantity).toHaveValue(String(--value));
        await tile.locator('.add-one').focus(); await page.keyboard.press('Space');
        await expect(quantity).toHaveValue(String(++value));
        await quantity.fill('11');await quantity.press('Tab');
        await expect(quantity).toHaveValue('11');
        await quantity.fill('50');await quantity.press('Tab');
        await tile.locator('.add-one').tap();
        await expect(quantity).toHaveValue('50');
        await minus.tap(); await expect(quantity).toHaveValue('49');
        await quantity.fill('0');await quantity.press('Tab');
        await minus.tap();
        await expect(page.locator('#refund-dialog')).toBeVisible();
        await expect(quantity).toHaveValue('0');
        await page.locator('#refund-cancel').click();
        await expect(quantity).toHaveValue('0');
        await minus.tap();await page.locator('#refund-confirm').click();
        await expect(quantity).toHaveValue('-1');
        await quantity.fill('-50');await quantity.press('Tab');
        await minus.tap(); await expect(quantity).toHaveValue('-50');
        await quantity.fill('0');await quantity.press('Tab');
        // Native gesture starts inside minus: vertical on phone, horizontal on tablet.
        const grid=page.locator('#product-grid');
        const dragMinus=page.locator('.remove-one').nth(viewport.width>850 ? 6 : 2);
        await dragMinus.scrollIntoViewIfNeeded();
        const buttonBox=await dragMinus.boundingBox();
        const gesture=await page.context().newCDPSession(page);
        const start={x:buttonBox.x+24,y:buttonBox.y+24};
        const beforeY=await page.evaluate(()=>scrollY);
        await gesture.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[start]});
        for(let i=1;i<=10;i++) {
            await gesture.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:start.x-(viewport.width>850?i*16:0),y:start.y-(viewport.width>850?0:i*16)}]});
            await page.waitForTimeout(20);
        }
        await gesture.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
        await gesture.detach();
        if(viewport.width>850) await expect.poll(()=>grid.evaluate(node=>node.scrollLeft)).toBeGreaterThan(10);
        else {
            await expect.poll(()=>page.evaluate(()=>scrollY)).toBeGreaterThan(beforeY);
            expect(await grid.evaluate(node=>node.scrollLeft)).toBe(0);
        }
        await expect(page.locator('#refund-dialog')).not.toBeVisible();
        await expect(page.locator('#total-yen')).toHaveText('0円');
        await expect(quantity).toHaveValue('0');
        await grid.evaluate(node=>node.scrollLeft=0);
        // A sustained press may produce a context menu or one click, never auto-repeat.
        await tile.locator('img').scrollIntoViewIfNeeded();
        const image=await tile.locator('img').boundingBox();
        const session=await page.context().newCDPSession(page);
        await session.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:image.x+image.width/2,y:image.y+image.height/2}]});
        await page.waitForTimeout(700);
        await session.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});
        await session.detach();
        expect([0,1]).toContain(Number(await quantity.inputValue()));
        expect(await page.evaluate(()=>indexedDB.databases())).toEqual([]);
        expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)).toBe(true);
        await context.close();
    });
}

test('Chromium: 画像なし・長い名前・上限価格も追加と修正の領域を分離',async({page},testInfo)=>{
    await page.setViewportSize({width:1024,height:768});
    await login(page);await page.goto('/products');
    await page.getByRole('link',{name:'＋ 新しい商品を登録'}).click();
    const name='焼きたてきのことチーズのパン・特別な長い商品名を確認するための商品';
    await page.locator('input[name=name]').fill(name);
    await page.locator('input[name=price_yen]').fill('100000');
    await page.locator('#product-editor button[type=submit]').click();
    await page.locator('#product-confirm-save').click();
    await expect(page).toHaveURL(/\/products/);
    await page.goto('/register/preview');
    const tile=page.locator('.product-tile').filter({has:page.getByRole('button',{name:`${name}を1個追加`,exact:true})});
    await expect(tile.locator('.no-image')).toBeVisible();
    for(const viewport of [{width:1024,height:768},{width:320,height:740}]) {
        await page.setViewportSize(viewport);await tile.scrollIntoViewIfNeeded();
        const fit=await tile.evaluate(node=>{
            const price=node.querySelector('.product-price');
            const range=document.createRange();range.selectNodeContents(price);
            const text=range.getBoundingClientRect(),box=node.getBoundingClientRect();
            const overlaps=[...node.querySelectorAll('.remove-one,.quantity-label')].some(control=>{
                const b=control.getBoundingClientRect();
                return text.left<b.right && text.right>b.left && text.top<b.bottom && text.bottom>b.top;
            });
            return {overlaps,font:getComputedStyle(price).fontSize,text:{left:text.left,right:text.right,top:text.top,bottom:text.bottom},controls:[...node.querySelectorAll('.remove-one,.quantity-label')].map(el=>{const b=el.getBoundingClientRect();return {left:b.left,right:b.right,top:b.top,bottom:b.bottom};}),inside:text.left>=box.left && text.right<=box.right,
                lines:node.querySelector('.product-label').clientHeight/parseFloat(getComputedStyle(node.querySelector('.product-label')).lineHeight)};
        });
        expect(fit.overlaps,JSON.stringify(fit)).toBe(false);expect(fit.inside).toBe(true);expect(fit.lines).toBeLessThanOrEqual(2.05);
        await page.screenshot({path:testInfo.outputPath(`long-price-${viewport.width}.png`),fullPage:true});
    }
    await tile.locator('.no-image').click();
    await expect(tile.locator('.quantity-input')).toHaveValue('1');
    await expect(page.locator('#total-yen')).toHaveText('100,000円');
    await tile.locator('.remove-one').click();
    await expect(tile.locator('.quantity-input')).toHaveValue('0');
});
