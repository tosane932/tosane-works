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
async function metrics(page) {
    return page.evaluate(()=>{
        const box=selector=>{const b=document.querySelector(selector).getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height,bottom:b.bottom};};
        return {viewport:[innerWidth,innerHeight,visualViewport.height],fit:document.body.classList.contains('register-fit'),
            overflow:document.documentElement.scrollHeight-innerHeight,
            rows:getComputedStyle(document.querySelector('.product-page')).gridTemplateRows.split(' ').length,
            tile:box('.product-tile'),image:box('.no-image'),key:box('[data-cash-key="7"]'),
            complete:box('#complete-checkout'),checkout:box('.checkout'),header:box('.register-header'), parts:[...document.querySelectorAll('.checkout > *, .checkout-payment > *, .cash-area > *')].map(n=>[n.className||n.id,n.getBoundingClientRect().height,getComputedStyle(n).marginTop,getComputedStyle(n).marginBottom])};
    });
}
for(const viewport of [{width:1024,height:600},{width:1024,height:768},{width:1280,height:720},{width:1280,height:800},{width:1536,height:960}]) {
    test(`touch tablet ${viewport.width}x${viewport.height}: two rows and full payment fit`,async({browser},info)=>{
        const context=await browser.newContext({viewport,hasTouch:true});const page=await context.newPage();
        await login(page);await page.goto('/register/preview');
        await expect(page.locator('.product-tile')).toHaveCount(2);
        await page.waitForTimeout(150);
        const m=await metrics(page);
        console.log('VIEWPORT',JSON.stringify(m));
        await info.attach('viewport.json',{body:JSON.stringify(m,null,2),contentType:'application/json'});
        expect(m.rows).toBe(2);expect(m.fit).toBe(true);expect(m.overflow).toBeLessThanOrEqual(1);
        expect(m.tile.w).toBeGreaterThanOrEqual(168);expect(Math.abs(m.tile.w-m.tile.h)).toBeLessThan(1);
        expect(m.image.h/m.tile.h).toBeGreaterThan(.5);
        expect(m.key.h).toBeGreaterThanOrEqual(52);expect(m.key.h).toBeLessThanOrEqual(60);
        expect(m.complete.h).toBeGreaterThanOrEqual(52);
        for(const selector of ['#total-yen','#received-display','#cash-keypad','#change-yen','#complete-checkout']) {
            await expect(page.locator(selector)).toBeInViewport({ratio:1});
            expect(await page.locator(selector).evaluate(el=>{const b=el.getBoundingClientRect();return el.contains(document.elementFromPoint(b.x+b.width/2,b.y+b.height/2));})).toBe(true);
        }
        await page.locator('.add-one').first().tap();await enterCash(page,'1000');
        await expect(page.locator('#change-yen')).toHaveText('880円');
        await page.screenshot({path:info.outputPath('tablet.png')});await context.close();
    });
}

for(const [viewport,rows] of [[{width:1280,height:800},3],[{width:1536,height:960},4]]) {
    test(`fine pointer PC ${viewport.width}x${viewport.height}: ${rows} readable rows`,async({browser},info)=>{
        const context=await browser.newContext({viewport,hasTouch:false});const page=await context.newPage();
        await login(page);await page.goto('/register/preview');
        await expect.poll(()=>page.locator('.product-page').first().evaluate(el=>getComputedStyle(el).gridTemplateRows.split(' ').length)).toBe(rows);
        const m=await metrics(page);console.log('PC VIEWPORT',JSON.stringify(m));
        await info.attach('viewport.json',{body:JSON.stringify(m,null,2),contentType:'application/json'});
        expect(m.tile.h).toBeGreaterThanOrEqual(168);expect(m.overflow).toBe(0);
        await page.locator('.add-one').first().focus();await page.keyboard.press('Space');
        await enterCash(page,'1000');await expect(page.locator('#change-yen')).toHaveText('880円');
        await context.close();
    });
}

test('touch: resize, short status and long warning settle without layout oscillation',async({browser})=>{
    const context=await browser.newContext({viewport:{width:1280,height:720},hasTouch:true});const page=await context.newPage();
    await login(page);await page.goto('/register/preview');await page.locator('.add-one').first().tap();await enterCash(page,'1000');
    await page.evaluate(()=>{
        window.fitChanges=0;window.tileNode=document.querySelector('.product-tile');
        new MutationObserver(records=>{window.fitChanges+=records.length;}).observe(document.body,{attributes:true,attributeFilter:['class']});
        const warning=document.querySelector('#register-message');warning.hidden=false;warning.textContent='未同期会計があります。端末に保存した会計を保持しています。';
    });
    await expect(page.locator('#register-layout-notice')).toBeHidden();
    await page.setViewportSize({width:1024,height:600});
    // This viewport can fit ordinary operations; extra status content can safely trigger fallback.
    await expect(page.locator('#register-message')).toBeVisible();
    await page.evaluate(()=>document.querySelector('#register-message').textContent='保存できません。会計を保持しています。'.repeat(80));
    await expect(page.locator('#register-layout-notice')).toBeVisible();
    await page.locator('#complete-checkout').scrollIntoViewIfNeeded();
    await expect(page.locator('#complete-checkout')).toBeInViewport({ratio:1});
    await page.evaluate(()=>document.querySelector('#register-message').hidden=true);
    await page.setViewportSize({width:1280,height:800});
    await expect(page.locator('#register-layout-notice')).toBeHidden();
    await expect.poll(()=>page.evaluate(()=>document.documentElement.scrollHeight-innerHeight)).toBe(0);
    await page.evaluate(()=>window.fitChanges=0);await page.waitForTimeout(600);
    expect(await page.evaluate(()=>window.fitChanges)).toBe(0);
    expect(await page.evaluate(()=>window.tileNode===document.querySelector('.product-tile'))).toBe(true);
    await expect(page.locator('.quantity-input').first()).toHaveValue('1');
    await expect(page.locator('#received-display')).toHaveText('1,000');
    await context.close();
});
