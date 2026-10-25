// Synthetic SQLite server only. Check the real browser layout and saved form values.
const {test, expect} = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const auth = () => JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR, 'auth.json')));

test.beforeEach(async ({context}) => {
    await context.route(/^https?:\/\//, route =>
        new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
});

for (const width of [390, 1280]) {
    test(`waste recording ${width}px: link, entry, edit and readable layout`, async ({page}) => {
        await page.setViewportSize({width, height:844});
        await page.goto('/login');
        await page.locator('#username').fill(auth().username);
        await page.locator('#password').fill(auth().password);
        await page.locator('#admin-login-form button[type=submit]').click();
        await page.goto(`/prep?date=${auth().day}`);
        await page.getByRole('link', {name:/製造実績・廃棄ロスを記録する/}).click();
        await expect(page).toHaveURL(/\/prep\/waste\?date=/);
        await expect(page.locator('.app-sidebar [data-page="prep"]')).toHaveAttribute('aria-current','page');
        await page.locator('#waste-product').selectOption({label:'検証クロワッサン'});
        const form = page.locator('form[data-waste-form]').first();
        await form.locator('[name=made_quantity]').fill('0');
        await form.locator('[name=discarded_quantity]').fill('3');
        await expect(form.locator('[data-save-state]')).toHaveText('未保存の変更があります');
        await form.getByRole('button', {name:'この商品を保存'}).click();
        await expect(page.locator('.waste-list .waste-card')).toHaveCount(1);
        const edit = page.locator('.waste-list .waste-card').first();
        await expect(edit.locator('[name=made_quantity]')).toHaveValue('0');
        await expect(edit.locator('[name=discarded_quantity]')).toHaveValue('3');
        await page.reload();
        await expect(page.locator('.waste-list [name=made_quantity]')).toHaveValue('0');
        await edit.locator('[name=made_quantity]').fill('50');
        await edit.getByRole('button', {name:'この記録を修正'}).click();
        await expect(page.locator('.waste-list .waste-card')).toHaveCount(1);
        await expect(page.locator('.waste-list [name=made_quantity]')).toHaveValue('50');
        await expect(page.locator('.waste-list [name=discarded_quantity]')).toHaveValue('3');
        await page.reload();
        await expect(page.locator('.waste-list [name=made_quantity]')).toHaveValue('50');
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
        const inputs = await page.locator('.waste-list .waste-fields input').evaluateAll(nodes =>
            nodes.map(node => ({right:node.getBoundingClientRect().right, width:node.getBoundingClientRect().width})));
        expect(inputs.every(input => input.width >= 100 && input.right <= width)).toBe(true);
        const screenshots = path.join(__dirname, '..', 'test-results', 'waste-screenshots');
        fs.mkdirSync(screenshots, {recursive:true});
        await page.screenshot({path:path.join(screenshots, `waste-${width}.png`), fullPage:true});
    });
}

test('guest cannot view the waste records', async ({page}) => {
    await page.goto('/login');
    await page.locator('#guest-demo-start').click();
    await page.goto('/prep/waste');
    await expect(page.locator('body')).not.toContainText('検証クロワッサン');
    expect((await page.locator('body').textContent()).includes('製造実績・廃棄ロスを記録する')).toBe(false);
});
