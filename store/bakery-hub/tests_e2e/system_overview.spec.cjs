// Public guide only; synthetic server and blocked external traffic.
const {test, expect} = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
test.setTimeout(30000);
const chapters = ['about','features','architecture','safety','textbook'];
test.beforeEach(async ({context}) => {
    await context.route(/^https?:\/\//, route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
});
async function openContents(page) {
    // The shared mobile bar intentionally hides on downward scroll.
    // Reveal it by scrolling upward, just as a reader does, before using it.
    await page.evaluate(() => scrollBy(0, -40));
    const button = page.locator('#overview-location-button');
    await expect(button).toBeVisible();
    await button.click();
}
for (const width of [390, 800, 1280]) {
    test(`public guide ${width}px: contents, focus, current location, resize and PDF`, async ({page}) => {
        await page.setViewportSize({width,height:844});
        await page.goto('/system-overview');
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        const button=page.locator('#overview-location-button');
        await expect(button).toBeVisible();
        await openContents(page);
        const panel=page.locator('#overview-page-contents');
        await expect(panel).toBeVisible();
        await expect(panel.locator('[aria-current=location]')).toBeFocused();
        await page.keyboard.press('Escape');
        await expect(button).toBeFocused();
        await expect(panel).toBeHidden();
        for (let i=0;i<chapters.length;i++) {
            await openContents(page);
            await panel.locator(`a[href="#${chapters[i]}"]`).click();
            await expect(page.locator(`#${chapters[i]} > h2`)).toBeFocused();
            await expect(button).toContainText(`${i+1}.`);
            const heading=await page.locator(`#${chapters[i]} > h2`).boundingBox();
            const bar=await button.boundingBox();
            expect(heading.y).toBeGreaterThanOrEqual(bar.y+bar.height-2);
        }
        await openContents(page);
        await panel.locator('[data-overview-close]').click();
        await expect(button).toBeFocused();
        await openContents(page);
        await page.locator('#overview-contents-backdrop').click({position:{x:8,y:700}});
        await expect(panel).toBeHidden();
        await page.emulateMedia({reducedMotion:'reduce'});
        await page.setViewportSize({width:width===390?844:390,height:844});
        await page.locator('html').evaluate(n=>n.style.fontSize='200%');
        await openContents(page);
        await panel.locator('a[href="#architecture"]').click();
        await expect(page.locator('#architecture > h2')).toBeFocused();
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        await page.locator('html').evaluate(n=>n.style.fontSize='');
        await page.setViewportSize({width,height:844});
        await page.screenshot({path:test.info().outputPath(`guide-${width}.png`),fullPage:true});
        const href=await page.locator('a[href$="bakery-hub-system-guide.pdf"]').getAttribute('href');
        const pdf=await page.request.get(href);
        expect(pdf.status()).toBe(200);expect(pdf.headers()['content-type']).toContain('application/pdf');
        expect((await pdf.body()).subarray(0,5).toString()).toBe('%PDF-');
    });
}

test('touch scrubber drag, keyboard and rotation retain chapter navigation', async ({browser})=>{
    const context=await browser.newContext({viewport:{width:390,height:844},hasTouch:true,isMobile:true});
    const page=await context.newPage();await page.goto('http://127.0.0.1:8765/system-overview');
    await page.evaluate(()=>scrollBy(0,300));
    const scrubber=page.locator('#overview-chapter-scrubber');
    const thumb=scrubber.locator('[role=slider]');
    await expect(scrubber).toHaveClass(/is-visible/);
    await expect(thumb).toHaveAttribute('aria-valuemax','5');
    const box=await thumb.boundingBox();const track=await scrubber.boundingBox();
    await page.mouse.move(box.x+22,box.y+22);await page.mouse.down();
    await page.mouse.move(box.x+22,track.y+track.height-22,{steps:8});
    await expect(page.locator('[data-scrubber-preview]')).toContainText('5.');
    await page.mouse.up();await expect(page).toHaveURL(/#textbook$/);
    await expect(page.locator('#textbook > h2')).toBeFocused();
    await page.evaluate(()=>scrollBy(0,-30));await thumb.focus();
    await page.keyboard.press('Home');await expect(page).toHaveURL(/#about$/);
    await page.keyboard.press('ArrowDown');await expect(page).toHaveURL(/#features$/);
    await page.keyboard.press('End');await expect(page).toHaveURL(/#textbook$/);
    await expect(thumb).toBeFocused();
    await page.setViewportSize({width:1000,height:600});
    await expect(scrubber).toBeHidden();
    await expect(page.locator('#overview-location-button')).toBeFocused();
    await context.close();
});

test('JS disabled: all chapters and ordinary anchors stay readable', async ({browser})=>{
    const context=await browser.newContext({javaScriptEnabled:false,viewport:{width:390,height:844}});
    const page=await context.newPage();await page.goto('http://127.0.0.1:8765/system-overview');
    await expect(page.locator('#overview-page-contents')).toBeVisible();
    await expect(page.locator('#overview-reader-navigation')).toBeHidden();
    await page.locator('a[href="#textbook"]').click();
    await expect(page.locator('#textbook h2')).toBeInViewport();
    expect(await page.locator('main details').count()).toBe(0);
    await context.close();
});

for (const principal of ['admin','guest']) {
    test(`${principal} can read the same guide and PDF without enabling features`,async ({page})=>{
        await page.goto('/login');
        if(principal==='guest') await page.locator('#guest-demo-start').click();
        else {
            const auth=JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR,'auth.json')));
            await page.locator('#username').fill(auth.username);await page.locator('#password').fill(auth.password);
            await page.locator('#admin-login-form button[type=submit]').click();
        }
        await expect(page).toHaveURL(/\/home/);await page.goto('/system-overview');
        await expect(page.locator('main > section[data-overview-chapter]')).toHaveCount(5);
        const response=await page.request.get('/static/docs/bakery-hub-system-guide.pdf');expect(response.status()).toBe(200);
    });
}
