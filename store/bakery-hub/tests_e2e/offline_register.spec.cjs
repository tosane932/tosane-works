const { test, expect, chromium, devices } = require('playwright/test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const base = 'http://127.0.0.1:8765';
const auth = () => JSON.parse(fs.readFileSync(path.join(process.env.E2E_STATE_DIR, 'auth.json')));
const control = (request, uri) => request.get(`${base}/__e2e/${uri}`, {
    headers: { 'X-E2E-Control': auth().control },
});
test.beforeEach(async ({ request }) => {
    expect((await control(request, 'reset-scenario')).ok()).toBe(true);
});
async function records(page) {
    return page.evaluate(async () => {
        const db = await new Promise((resolve, reject) => {
            const open = indexedDB.open('bakery-hub-register-v3', 2);
            open.onsuccess = () => resolve(open.result);
            open.onerror = () => reject(open.error);
        });
        try {
            return await new Promise((resolve, reject) => {
                const tx = db.transaction('queue', 'readonly');
                const req = tx.objectStore('queue').getAll();
                req.onsuccess = () => resolve(req.result);
                req.onerror = () => reject(req.error);
            });
        } finally { db.close(); }
    });
}
async function login(page) {
    await page.goto(`${base}/login`);
    await page.locator('#username').fill(auth().username);
    await page.locator('#password').fill(auth().password);
    await page.locator('#admin-login-form button[type=submit]').click();
    await expect(page).toHaveURL(/\/home/);
}
async function prepare(page) {
    await page.goto(`${base}/register/preview`);
    await expect(page.locator('#offline-setup')).toBeVisible();
    await page.locator('#offline-next-day').check();
    await page.locator('#offline-setup-pin').fill('e2e-pin-not-a-secret');
    await page.locator('#offline-setup button[type=submit]').click();
    await expect(page.locator('#offline-setup-state')).toContainText('端末を準備しました');
    const registration = await page.evaluate(async () => {
        const sw = await navigator.serviceWorker.ready;
        const keys = await caches.keys();
        const cache = await caches.open('register-offline-v1');
        return { scope: sw.scope, shell: Boolean(await cache.match('/register/offline')),
                 preview: Boolean(await cache.match('/register/preview')),
                 keys };
    });
    expect(registration.scope).toBe(`${base}/register/`);
    expect(registration.shell).toBe(true);
    expect(registration.preview).toBe(false);
    await page.locator('#offline-setup-state a').click();
    await unlock(page);
}
async function unlock(page) {
    await page.locator('#offline-pin').fill('wrong-pin');
    await page.locator('#offline-unlock button').click();
    await expect(page.locator('#offline-unlock-state')).toContainText('PIN');
    await expect(page.locator('#product-grid .product-tile')).toHaveCount(0);
    await page.locator('#offline-pin').fill('e2e-pin-not-a-secret');
    await page.locator('#offline-unlock button').click();
    await expect(page.locator('#offline-unlock-state')).toContainText('端末内レジを開きました');
    await expect(page.locator('.product-tile', { hasText: '検証クロワッサン' })).toHaveCount(1);
    await expect(page.locator('.product-tile', { hasText: '検証食パン' })).toHaveCount(1);
    await expect(page.locator('#complete-checkout')).toBeDisabled();
}
async function sale(page, bread = false) {
    const tile = page.locator('.product-tile', { hasText: '検証クロワッサン' });
    await tile.locator('.add-one').click({ clickCount: 2 });
    if (bread) { await page.locator('.product-tile', { hasText: '検証食パン' }).locator('.add-one').click(); }
    await expect(page.locator('#total-yen')).toHaveText(bread ? '540円' : '240円');
    await page.locator('#received-yen').fill('1000');
    await expect(page.locator('#change-yen')).toHaveText(bread ? '460円' : '760円');
    await page.locator('#complete-checkout').click();
    await expect(page.locator('#checkout-result')).toContainText('端末に記録済み');
    await expect(page.locator('#total-yen')).toHaveText('0円');
}
async function refund(page) {
    await page.locator('.product-tile', { hasText: '検証クロワッサン' }).locator('.remove-one').click();
    await expect(page.locator('#refund-dialog')).toBeVisible();
    await page.locator('#refund-confirm').click();
    await expect(page.locator('#total-yen')).toHaveText('120円');
    await page.locator('#complete-checkout').click();
    await expect(page.locator('#checkout-result')).toContainText('端末に記録済み');
}

test('Chromium: 通信断の連続会計、再読み込み、プロファイル再起動、翌日と価格改定後の同期', async ({ request }) => {
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'bakery-register-profile-'));
    let context = await chromium.launchPersistentContext(profile, { headless: true });
    try {
        let page = context.pages()[0] || await context.newPage();
        await login(page);
        await prepare(page);
        await context.setOffline(true);
        await sale(page, true);
        await sale(page);
        await refund(page);
        expect(await records(page)).toHaveLength(3);
        await page.reload({ waitUntil: 'domcontentloaded' });
        await unlock(page);
        await expect(page.locator('#sync-state')).toContainText('3件');
        await sale(page);
        const prior = await records(page);
        expect(prior).toHaveLength(4);
        await context.close();

        context = await chromium.launchPersistentContext(profile, { headless: true, offline: true });
        page = context.pages()[0] || await context.newPage();
        await page.goto(`${base}/register/offline`, { waitUntil: 'domcontentloaded' });
        await unlock(page);
        expect((await records(page)).map(item => item.receipt.submission_key))
            .toEqual(prior.map(item => item.receipt.submission_key));
        await sale(page);
        const local = await records(page);
        expect(local).toHaveLength(5);
        expect(new Set(local.map(item => item.receipt.submission_key)).size).toBe(5);
        expect(local.every(item => item.status === 'pending')).toBe(true);
        expect((await control(request, 'state')).ok()).toBe(true);
        expect(await (await control(request, 'state')).json()).toHaveLength(0);
        expect((await control(request, 'change-price')).ok()).toBe(true);
        const next = await control(request, 'next-day');
        expect((await next.json()).day).not.toBe(auth().day);

        // The first server commit succeeds but its HTTP response is lost.
        let lost = false;
        await page.route('**/api/register/offline/sync', async route => {
            if (!lost) { lost = true; await route.fetch(); await route.abort(); }
            else { await route.continue(); }
        });
        await context.setOffline(false);
        await expect.poll(() => page.evaluate(() => navigator.onLine)).toBe(true);
        // Persistent contexts launched offline do not always dispatch the browser's
        // online event when Playwright lifts network emulation. Deliver that event.
        await page.evaluate(() => window.dispatchEvent(new Event('online')));
        await expect(page.locator('#offline-unlock-state')).toContainText('再ログイン');
        expect(await (await control(request, 'state')).json()).toHaveLength(0);
        expect(await records(page)).toHaveLength(5);
        const loginPage = await context.newPage();
        await login(loginPage);
        await loginPage.close();
        await page.reload();
        await unlock(page);
        await expect.poll(async () => (await (await control(request, 'state')).json()).length,
                          { timeout: 30000 }).toBe(5);
        await page.locator('#check-register-status').click();
        await expect.poll(async () => (await records(page)).filter(item => item.status === 'synced').length,
                          { timeout: 30000 }).toBe(5);
        const saved = await (await control(request, 'state')).json();
        expect(new Set(saved.map(item => item.key)).size).toBe(5);
        expect(saved.every(item => item.day === auth().day && item.method === 'offline')).toBe(true);
        expect(saved.flatMap(item => item.lines).filter(line => line.name === '検証クロワッサン')
            .every(line => line.price === 120)).toBe(true);
        expect(saved.some(item => item.total === -120)).toBe(true);
        expect((await records(page)).length).toBe(5); // synced receipts are retained.
    } finally { await context.close(); }
});

for (const [name, viewport] of [['smartphone', devices['iPhone 13'].viewport],
                                 ['landscape tablet', { width: 1024, height: 768 }]]) {
    test(`Chromium ${name}: prepared catalog, touch targets and local completion`, async ({ browser }) => {
        const context = await browser.newContext({ viewport, isMobile: name === 'smartphone', hasTouch: true });
        try {
            const page = await context.newPage();
            await login(page);
            await prepare(page);
            // Layout contract: no horizontal overflow or crowded touch targets.
            // A phone uses full-width, horizontal product rows; tablet retains a grid.
            const layout = await page.evaluate(() => {
                const tiles = [...document.querySelectorAll('.product-tile')];
                const first = tiles[0].getBoundingClientRect();
                const second = tiles[1].getBoundingClientRect();
                const targets = [...document.querySelectorAll(
                    '.product-tile .add-one, .product-tile .remove-one, .product-tile .quantity-input'
                )].map(element => element.getBoundingClientRect());
                return {
                    scrollWidth: document.documentElement.scrollWidth,
                    viewportWidth: document.documentElement.clientWidth,
                    sameRow: Math.abs(first.top - second.top) < 4,
                    minTargetWidth: Math.min(...targets.map(box => box.width)),
                    minTargetHeight: Math.min(...targets.map(box => box.height)),
                    imageWidth: tiles[0].querySelector('img')?.getBoundingClientRect().width,
                };
            });
            expect(layout.scrollWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
            expect(layout.minTargetWidth).toBeGreaterThanOrEqual(44);
            expect(layout.minTargetHeight).toBeGreaterThanOrEqual(44);
            if (name === 'smartphone') {
                expect(layout.sameRow).toBe(false);
                expect(layout.imageWidth).toBeGreaterThanOrEqual(90);
            } else {
                expect(layout.sameRow).toBe(true);
            }
            await context.setOffline(true);
            await sale(page);
            expect((await records(page))[0].status).toBe('pending');
            await expect(page.locator('#sync-state')).toContainText('1件');
        } finally { await context.close(); }
    });
}

test('Chromium: 100件は同期し101件目は元記録を残して要確認', async ({ browser, request }) => {
    test.setTimeout(300000);
    const existing = (await (await control(request, 'state')).json()).length;
    const context = await browser.newContext();
    try {
        const page = await context.newPage();
        await login(page);
        await prepare(page);
        await context.setOffline(true);
        for (let index = 0; index < 101; index++) { await sale(page); }
        const local = await records(page);
        expect(local).toHaveLength(101);
        expect(new Set(local.map(item => item.receipt.submission_key)).size).toBe(101);
        await context.setOffline(false);
        await expect.poll(async () => (await records(page)).filter(item => item.status === 'synced').length,
                          { timeout: 180000 }).toBe(100);
        await expect.poll(async () => (await records(page)).filter(item => item.status === 'needs_review').length,
                          { timeout: 30000 }).toBe(1);
        const final = await records(page);
        expect(final).toHaveLength(101);
        expect(final.filter(item => item.status === 'needs_review')[0].reason).toBe('offline_review');
        expect(await (await control(request, 'state')).json()).toHaveLength(existing + 100);
    } finally { await context.close(); }
});

test('Chromium: IndexedDBへの会計書込失敗ではカートを維持', async ({ browser }) => {
    const context = await browser.newContext();
    try {
        const page = await context.newPage();
        await login(page);
        await prepare(page);
        await context.setOffline(true);
        await page.locator('.product-tile', { hasText: '検証クロワッサン' }).locator('.add-one').click();
        await page.locator('#received-yen').fill('500');
        await page.evaluate(() => {
            const original = IDBDatabase.prototype.transaction;
            IDBDatabase.prototype.transaction = function (names, mode, ...args) {
                if (Array.isArray(names) && names.includes('queue') && mode === 'readwrite') {
                    throw new DOMException('no space', 'QuotaExceededError');
                }
                return original.call(this, names, mode, ...args);
            };
        });
        await page.locator('#complete-checkout').click();
        await expect(page.locator('#register-message')).toContainText('入力内容を保持しています');
        await expect(page.locator('#total-yen')).toHaveText('120円');
        await expect(page.locator('#received-yen')).toHaveValue('500');
        expect(await records(page)).toHaveLength(0);
    } finally { await context.close(); }
});

test('Chromium: Adminログアウト後はキャッシュ済みシェルからPINで会計を再開できない', async ({ browser }) => {
    const context = await browser.newContext();
    try {
        const page = await context.newPage();
        await login(page);
        await prepare(page);
        await page.goto(`${base}/home`);
        await expect(page.locator('#app-logout-form')).toHaveCount(1);
        await page.locator('#app-logout-form').evaluate(form => form.requestSubmit());
        await expect(page).toHaveURL(/\/login/);
        await context.setOffline(true);
        await page.goto(`${base}/register/offline`, { waitUntil: 'domcontentloaded' });
        await page.locator('#offline-pin').fill('e2e-pin-not-a-secret');
        await page.locator('#offline-unlock button').click();
        await expect(page.locator('#offline-unlock-state')).toContainText('ログアウト後');
        await expect(page.locator('#product-grid .product-tile')).toHaveCount(0);
        expect(await records(page)).toHaveLength(0);
    } finally { await context.close(); }
});
