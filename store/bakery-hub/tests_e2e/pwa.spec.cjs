// Manifest/browser checks are not a real Android installation or standalone launch.
const { test, expect } = require('playwright/test');
const fs = require('node:fs');
const path = require('node:path');
const { enterCash, openTools } = require('./register_helpers.cjs');
const auth = () => JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR, 'auth.json')));

test.beforeEach(async ({context}) => {
    // Navigation tests need no external CDN/weather service; keep them loopback-only.
    await context.route(/^https?:\/\//, route =>
        new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort());
});

async function login(page) {
    await page.goto('/home');
    await expect(page).toHaveURL(/\/login/);
    await page.locator('#username').fill(auth().username);
    await page.locator('#password').fill(auth().password);
    await page.locator('#admin-login-form button[type=submit]').click();
    await expect(page).toHaveURL(/\/home/);
}
async function assertManifest(page) {
    await expect(page.locator('head link[rel=manifest]')).toHaveAttribute('href', '/static/manifest.webmanifest');
    const cdp = await page.context().newCDPSession(page);
    const manifest = await cdp.send('Page.getAppManifest');
    expect(manifest.errors).toEqual([]);
    expect(JSON.parse(manifest.data)).toMatchObject({
        name:'Bakery Hub', id:'/bakery-hub', start_url:'/home', scope:'/', display:'standalone', lang:'ja',
    });
    await cdp.detach();
}

test('one app manifest across login, business links, demo, back, reload and logout', async ({page, context}) => {
    await page.setViewportSize({width:1280, height:800});
    await login(page);
    await assertManifest(page);
    // This runner is a browser tab: do not count it as an installed standalone window.
    expect(await page.evaluate(() => matchMedia('(display-mode: browser)').matches)).toBe(true);
    for (const target of ['/dashboard', '/products', '/prep', '/staff-shifts', '/schedule',
                          '/material-orders', '/shop-tools/memo', '/shop-tools/tasks', '/register/demo']) {
        await page.goto('/home');
        if (target.startsWith('/shop-tools/')) {
            await page.locator('a[href="/material-orders"]:visible').first().click();
        }
        if (target === '/register/demo') {
            await page.locator('#app-register-management summary').click();
        }
        const previous = page.url();
        await page.locator(`a[href="${target}"]:visible`).first().click();
        await expect(page).toHaveURL(new RegExp(`${target}$`));
        await assertManifest(page);
        await page.reload();
        await assertManifest(page);
        await page.goBack();
        expect(page.url()).toBe(previous);
    }
    expect(context.pages()).toHaveLength(1);
    expect(await page.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).length)).toBe(0);
    expect(await page.evaluate(() => caches.keys())).toEqual([]);
    await page.locator('form[action="/logout"] button').click();
    await expect(page).toHaveURL(/\/login$/);
    await assertManifest(page);
    await login(page);
    // Expiry or missing session follows the existing login route.
    await context.clearCookies();
    await page.reload();
    await expect(page).toHaveURL(/\/login/);
});

test('phone: public metadata never creates cached authenticated HTML or API fallback', async ({page, context}) => {
    await page.setViewportSize({width:390, height:844});
    await login(page);
    await assertManifest(page);
    await page.request.get('/api/dashboard-data');
    await context.setOffline(true);
    const results = await page.evaluate(async () => {
        const urls = ['/login', '/home', '/api/dashboard-data', '/api/register/offline/session'];
        return Promise.all(urls.map(async url => {
            try { await fetch(url, {cache:'no-store'}); return 'unexpected response'; }
            catch (_) { return 'network failed'; }
        }));
    });
    expect(results).toEqual(Array(4).fill('network failed'));
    expect(await page.evaluate(() => caches.keys())).toEqual([]);
    await context.setOffline(false);
    await page.reload();
    await expect(page).toHaveURL(/\/home$/);
});

test('same app metadata preserves register worker scope and an unsynced receipt across home/reload', async ({page, context, request}) => {
    expect((await request.get('/__e2e/reset-scenario', {headers:{'X-E2E-Control':auth().control}})).ok()).toBe(true);
    await login(page);
    await page.goto('/register/preview');
    await openTools(page);
    await page.locator('#offline-setup-pin').fill('e2e-pin-not-a-secret');
    await page.locator('#offline-setup button[type=submit]').click();
    await expect(page.locator('#offline-setup-state')).toContainText('端末を準備しました');
    await page.locator('#offline-setup-state a').click();
    await page.locator('#offline-pin').fill('e2e-pin-not-a-secret');
    await page.locator('#offline-unlock button').click();
    await expect(page.locator('#offline-unlock-state')).toContainText('端末内レジを開きました');
    await assertManifest(page);
    const worker = await page.evaluate(async () => ({
        scopes:(await navigator.serviceWorker.getRegistrations()).map(item => new URL(item.scope).pathname),
        script:new URL(navigator.serviceWorker.controller.scriptURL).pathname,
    }));
    expect(worker).toEqual({scopes:['/register/'], script:'/register/offline-sw.js'});
    await context.setOffline(true);
    await page.locator('.product-tile', {hasText:'検証クロワッサン'}).locator('.add-one').click();
    await enterCash(page, '200');
    await page.locator('#complete-checkout').click();
    await expect(page.locator('#sync-state')).toContainText('未同期');
    const readQueue = () => page.evaluate(async () => {
        const database = await new Promise((resolve, reject) => {
            const open = indexedDB.open('bakery-hub-register-v3', 2);
            open.onsuccess = () => resolve(open.result); open.onerror = () => reject(open.error);
        });
        try { return await new Promise((resolve, reject) => {
            const query = database.transaction('queue').objectStore('queue').getAll();
            query.onsuccess = () => resolve(query.result); query.onerror = () => reject(query.error);
        }); } finally { database.close(); }
    });
    const receipt = await readQueue();
    expect(receipt).toHaveLength(1);
    // Restore connectivity with sync blocked to observe the exact pending record.
    await page.route('**/api/register/offline/sync', route => route.abort());
    await context.setOffline(false);
    await page.goto('/home');
    await assertManifest(page);
    expect(await page.evaluate(() => navigator.serviceWorker.controller)).toBeNull();
    await page.reload();
    const unchangedReceipt = async () => {
        const rows = await readQueue();
        expect(rows).toHaveLength(1);
        expect(rows[0].id).toBe(receipt[0].id);
        expect(rows[0].receipt).toEqual(receipt[0].receipt);
        expect(rows[0].grantToken).toBe(receipt[0].grantToken);
        expect(rows[0].status).toBe('pending');
    };
    await unchangedReceipt();
    await page.goto('/register/offline');
    await page.reload();
    await unchangedReceipt();
    const cachedPaths = await page.evaluate(async () => {
        const paths = [];
        for (const name of await caches.keys()) {
            for (const item of await (await caches.open(name)).keys()) paths.push(new URL(item.url).pathname);
        }
        return paths;
    });
    expect(cachedPaths).toContain('/register/offline');
    expect(cachedPaths.some(item => ['/home','/login','/register/preview'].includes(item) || item.startsWith('/api/'))).toBe(false);
    await context.setOffline(true);
    await page.reload();
    await expect(page.locator('#offline-unlock')).toBeVisible();
    await unchangedReceipt();
});
