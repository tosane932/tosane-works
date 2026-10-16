const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const path = require('node:path');
const core = require('../static/register_core.js');

const source = fs.readFileSync(path.join(__dirname, '../static/register.js'), 'utf8');
const products = [{ id: 1, name: 'パン', price: 120, image: null, standard: true }];

class MemoryDB {
    constructor() {
        this.stores = new Map();
        this.objectStoreNames = { contains: name => this.stores.has(name) };
        this.failQueue = false;
        this.failDraft = false;
    }
    createObjectStore(name) {
        this.stores.set(name, new Map());
        return { createIndex() {} };
    }
    transaction(names) {
        const db = this;
        const tx = { oncomplete: null, onerror: null, onabort: null, pending: 0, closed: false };
        const finish = () => {
            if (tx.pending || tx.closed) { return; }
            setImmediate(() => {
                if (!tx.pending && !tx.closed) { tx.closed = true; tx.oncomplete?.(); }
            });
        };
        const request = fn => {
            tx.pending++;
            const item = { onsuccess: null, result: null };
            queueMicrotask(() => {
                try {
                    item.result = fn();
                    item.onsuccess?.();
                } catch (error) {
                    tx.closed = true;
                    tx.error = error;
                    tx.onabort?.();
                } finally { tx.pending--; finish(); }
            });
            return item;
        };
        tx.objectStore = name => {
            const store = db.stores.get(name);
            return {
                get: key => request(() => store.get(key)),
                getAll: () => request(() => [...store.values()]),
                put: value => request(() => {
                    if (name === 'drafts' && db.failDraft) { throw Error('draft quota'); }
                    store.set(value.id || value.scope, structuredClone(value));
                    return value;
                }),
                add: value => request(() => {
                    if (db.failQueue || store.has(value.id)) { throw Error('quota / duplicate'); }
                    store.set(value.id, structuredClone(value));
                }),
                delete: key => request(() => store.delete(key)),
                index: () => ({ getAll: scope => request(() => [...store.values()]
                    .filter(value => value.scope === scope)) }),
            };
        };
        return tx;
    }
    open() {
        const request = { result: this, onupgradeneeded: null, onsuccess: null };
        queueMicrotask(() => { request.onupgradeneeded?.(); request.onsuccess?.(); });
        return request;
    }
}

function element(id) {
    const listeners = {};
    return {
        id, hidden: false, disabled: false, value: '', textContent: '', children: [],
        dataset: {}, classList: { toggle() {} },
        addEventListener(type, handler) { listeners[type] = handler; },
        fire(type, event = {}) { return listeners[type]?.(event); },
        append(...items) { this.children.push(...items); },
        replaceChildren() { this.children = []; },
        querySelectorAll() { return []; },
        showModal() { this.open = true; }, close() { this.open = false; },
    };
}

function harness({ database = new MemoryDB(), scope = 'dataset:login', online = false,
                   fetcher, sessionValues = new Map() } = {}) {
    const ids = ['register-root', 'register-config', 'register-message',
        'complete-checkout', 'received-yen', 'sync-state', 'recovery-list', 'product-grid',
        'cart-lines', 'checkout-kind', 'total-label', 'total-yen', 'cash-area',
        'jump-checkout', 'checkout-title', 'check-register-status', 'status-check-result',
        'change-yen', 'cash-hint', 'refund-dialog', 'refund-dialog-detail',
        'refund-cancel', 'refund-confirm', 'register-back', 'discard-dialog',
        'discard-cancel', 'discard-confirm'];
    const elements = Object.fromEntries(ids.map(id => [id, element(id)]));
    elements['register-config'].textContent = JSON.stringify({
        products, scope, saveEnabled: true, postUrl: '/api/register/transactions',
        statusBase: '/api/register/transactions',
    });
    elements['register-back'].href = '/home';
    elements['checkout-title'].scrollIntoView = () => {};
    const quantity = element('quantity');
    const tile = { dataset: { productId: '1' }, classList: { toggle() {} },
        querySelector: () => quantity };
    quantity.matches = selector => selector === '.quantity-input';
    quantity.closest = () => tile;
    elements['product-grid'].children.push(tile);
    const plus = { dataset: { action: 'plus' }, closest: selector =>
        selector === 'button[data-action]' ? plus : tile };
    const events = {};
    const document = {
        hidden: false,
        getElementById: id => elements[id],
        querySelector: () => ({ content: 'csrf-test-token' }),
        createElement: () => element('created'),
        addEventListener: (type, handler) => { events[`document:${type}`] = handler; },
    };
    const window = { addEventListener: (type, handler) => { events[type] = handler; } };
    const navigator = { onLine: online,
        locks: { request: async (_name, _options, callback) => callback({}) } };
    const calls = [];
    const fetch = async (_url, options) => {
        const call = options.method === 'GET'
            ? { method: 'GET', url: _url } : JSON.parse(options.body);
        calls.push(call);
        if (fetcher) { return fetcher(call); }
        if (call.method === 'GET') {
            const local = [...database.stores.get('queue').values()]
                .find(row => call.url.includes(row.receipt.submission_key))?.receipt;
            return { ok: true, status: 200, headers: { get: () => 'application/json' },
                json: async () => ({ status: 'saved', submission_key: local.submission_key,
                    transaction_id: 'saved-id', kind: local.kind,
                    business_date: local.client_business_date, total_yen: local.total_yen,
                    received_yen: local.received_yen, items: local.items,
                    displayed_prices: local.displayed_prices }) };
        }
        return { ok: true, status: 200,
            headers: { get: () => 'application/json' },
            json: async () => ({ submission_key: calls.at(-1).submission_key,
                transaction_id: 'saved-id', total_yen: 120,
                business_date: calls.at(-1).client_business_date }) };
    };
    vm.runInNewContext(source, {
        document, window, indexedDB: { open: () => database.open() },
        RegisterCore: core, crypto: webcrypto, navigator, fetch, location: { assign() {} },
        sessionStorage: { getItem: name => sessionValues.get(name),
                          setItem: (name, value) => sessionValues.set(name, value) },
        setTimeout, clearTimeout, AbortController, Date, Intl, Promise, console,
    }, { filename: 'register.js' });
    return { database, elements, events, navigator, calls, plus, quantity, sessionValues,
        touch: () => elements['product-grid'].fire('click', { target: plus }) };
}

async function until(condition, timeout = 1200) {
    const started = Date.now();
    while (!condition()) {
        if (Date.now() - started > timeout) { throw Error('condition timed out'); }
        await new Promise(resolve => setTimeout(resolve, 5));
    }
}

test('オフライン会計はIndexedDBへ保存して次へ進み、復旧後に1回同期する', async () => {
    const ui = harness();
    await until(() => ui.elements['sync-state'].textContent === '同期済み');
    ui.touch();
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    assert.equal(ui.elements['complete-checkout'].disabled, false);
    assert.equal(ui.calls.length, 0); // タッチ／金額入力時にHTTPなし。
    ui.elements['complete-checkout'].fire('click');
    await until(() => ui.database.stores.get('queue').size === 1);
    await until(() => ui.elements['sync-state'].textContent === '端末に保存済み・サーバー反映未確認 1件（通信後に再送）');
    assert.equal(ui.elements['product-grid'].children[0].querySelector().value, 0);
    assert.equal(ui.calls.length, 0);
    ui.navigator.onLine = true;
    ui.events.online();
    await until(() => [...ui.database.stores.get('queue').values()][0].status === 'synced');
    assert.equal(ui.calls.filter(call => call.method !== 'GET').length, 1);
    assert.equal(ui.calls.filter(call => call.method === 'GET').length, 1);
    assert.equal(ui.elements['sync-state'].textContent, '同期済み');
    assert.equal(ui.calls[0].submission_key,
        [...ui.database.stores.get('queue').values()][0].receipt.submission_key);
});

test('IndexedDB保存失敗で未保存と表示し、カートを消去しない', async () => {
    const db = new MemoryDB();
    const ui = harness({ database: db, online: true });
    await until(() => db.stores.has('queue'));
    db.failQueue = true;
    ui.touch();
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    ui.elements['complete-checkout'].fire('click');
    await until(() => ui.elements['register-message'].textContent.includes('会計記録を保存できませんでした'));
    assert.equal(ui.elements['product-grid'].children[0].querySelector().value, 1);
    assert.equal(db.stores.get('queue').size, 0);
    assert.equal(ui.calls.length, 0);
});

test('価格差や不正応答は要確認として残し、別Datasetには表示しない', async () => {
    const db = new MemoryDB();
    const ui = harness({ database: db, online: true,
        fetcher: async () => ({ status: 409, ok: false,
            headers: { get: () => 'application/json' },
            json: async () => ({ code: 'needs_review' }) }) });
    await until(() => db.stores.has('queue'));
    ui.touch();
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    ui.elements['complete-checkout'].fire('click');
    await until(() => [...db.stores.get('queue').values()][0]?.status === 'needs_review');
    await until(() => ui.elements['sync-state'].textContent ===
        '確認が必要な会計があります 1件 / 未同期 0件');
    assert.equal(ui.elements['sync-state'].textContent, '確認が必要な会計があります 1件 / 未同期 0件');
    const other = harness({ database: db, scope: 'other-dataset:other-session', online: true });
    await until(() => other.elements['sync-state'].textContent === '同期済み');
    assert.equal(other.calls.length, 0);
    assert.equal(db.stores.get('queue').size, 1);
});

test('直接入力の負数は確認後だけ反映し、戻ると未同期キューは残る', async () => {
    const ui = harness();
    await until(() => ui.database.stores.has('drafts'));
    ui.quantity.value = '-1';
    ui.elements['product-grid'].fire('change', { target: ui.quantity });
    assert.equal(ui.elements['refund-dialog'].open, true);
    assert.equal(ui.quantity.value, 0);
    ui.elements['refund-confirm'].fire('click');
    assert.equal(ui.quantity.value, -1);
    assert.equal(ui.elements['checkout-kind'].textContent.includes('返金会計'), true);
    assert.equal(ui.elements['cash-area'].hidden, true);
    ui.elements['complete-checkout'].fire('click');
    await until(() => ui.database.stores.get('queue').size === 1);
    await until(() => ui.elements['sync-state'].textContent === '端末に保存済み・サーバー反映未確認 1件（通信後に再送）');
    let prevented = false;
    ui.elements['register-back'].fire('click', { preventDefault: () => { prevented = true; } });
    assert.equal(prevented, false); // 確定済み未同期レコードは入力中カートではない。
    assert.equal(ui.database.stores.get('queue').size, 1);
});

test('再読み込み後の下書きを同じ会計キーで復元する', async () => {
    const db = new MemoryDB();
    const tabs = new Map();
    const first = harness({ database: db, sessionValues: tabs });
    await until(() => db.stores.has('drafts'));
    first.touch();
    await until(() => db.stores.get('drafts').size === 1);
    const original = [...db.stores.get('drafts').values()][0];
    const reload = harness({ database: db, sessionValues: tabs });
    await until(() => reload.quantity.value === 1);
    assert.equal([...db.stores.get('drafts').values()][0].key, original.key);
    assert.equal(reload.elements['checkout-kind'].textContent, '販売会計');
});

test('同期失敗後の再送は同じ送信キーを保持する', async () => {
    let attempts = 0;
    const ui = harness({ online: true, fetcher: async body => {
        if (body.method === 'GET') {
            const local = [...ui.database.stores.get('queue').values()][0].receipt;
            return { status: 200, ok: true, headers: { get: () => 'application/json' },
                json: async () => ({ status: 'saved', submission_key: local.submission_key,
                    transaction_id: 'saved', kind: local.kind, total_yen: local.total_yen,
                    received_yen: local.received_yen, business_date: local.client_business_date,
                    items: local.items, displayed_prices: local.displayed_prices }) };
        }
        if (++attempts === 1) { throw Error('offline'); }
        return { status: 200, ok: true, headers: { get: () => 'application/json' },
            json: async () => ({ submission_key: body.submission_key,
                transaction_id: 'saved', total_yen: 120,
                business_date: body.client_business_date }) };
    } });
    await until(() => ui.database.stores.has('queue'));
    ui.touch();
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    ui.elements['complete-checkout'].fire('click');
    await until(() => [...ui.database.stores.get('queue').values()][0]?.attempts === 1);
    const record = [...ui.database.stores.get('queue').values()][0];
    record.nextRetryAt = 0;
    ui.database.stores.get('queue').set(record.id, record);
    await new Promise(resolve => setTimeout(resolve, 15));
    ui.events.online();
    await until(() => [...ui.database.stores.get('queue').values()][0].status === 'synced');
    assert.equal(ui.calls.filter(call => call.method !== 'GET').length, 2);
    assert.equal(ui.calls[0].submission_key, ui.calls[1].submission_key);
    assert.equal(ui.calls[2].method, 'GET');
});

test('HTML応答は保存成功と判断せず、端末の記録を残す', async () => {
    const ui = harness({ online: true, fetcher: async () => ({
        status: 200, ok: true, headers: { get: () => 'text/html' },
    }) });
    await until(() => ui.database.stores.has('queue'));
    ui.touch();
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    ui.elements['complete-checkout'].fire('click');
    await until(() => [...ui.database.stores.get('queue').values()][0]?.status === 'auth_required');
    assert.equal(ui.database.stores.get('queue').size, 1);
    assert.match(ui.elements['sync-state'].textContent, /認証の確認が必要/);
});

test('通信断の複数会計を順番に同じキーで再送し、応答確認後だけ同期済みにする', async () => {
    const ui = harness({ online: false });
    await until(() => ui.database.stores.has('queue'));
    for (let i = 0; i < 3; i++) {
        ui.touch();
        ui.elements['received-yen'].value = '500';
        ui.elements['received-yen'].fire('input');
        ui.elements['complete-checkout'].fire('click');
        await until(() => ui.database.stores.get('queue').size === i + 1);
        await until(() => ui.elements['product-grid'].children[0].querySelector().value === 0);
    }
    const keys = [...ui.database.stores.get('queue').values()].map(row => row.receipt.submission_key);
    assert.equal(new Set(keys).size, 3);
    assert.equal(ui.calls.length, 0);
    ui.navigator.onLine = true;
    ui.events.online();
    await until(() => [...ui.database.stores.get('queue').values()].every(row => row.status === 'synced'));
    assert.deepEqual(ui.calls.filter(row => row.method !== 'GET').map(row => row.submission_key), keys);
    assert.equal(ui.calls.filter(row => row.method === 'GET').length, 3);
    assert.equal(ui.database.stores.get('queue').size, 3);
});

test('営業日跨ぎは送信せず、理由と元記録を保持する', async () => {
    const ui = harness({ online: false });
    await until(() => ui.database.stores.has('queue'));
    ui.touch();
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    ui.elements['complete-checkout'].fire('click');
    await until(() => ui.database.stores.get('queue').size === 1);
    const original = [...ui.database.stores.get('queue').values()][0];
    original.receipt.client_business_date = '2020-01-01';
    ui.database.stores.get('queue').set(original.id, structuredClone(original));
    ui.navigator.onLine = true;
    ui.events.online();
    await until(() => [...ui.database.stores.get('queue').values()][0].status === 'needs_review');
    const saved = [...ui.database.stores.get('queue').values()][0];
    assert.equal(saved.reason, 'business_day_changed');
    assert.equal(saved.receipt.submission_key, original.receipt.submission_key);
    assert.equal(ui.calls.length, 0);
    assert.match(ui.elements['recovery-list'].textContent, /営業日/);
});

test('価格競合は差額を表示し、送信キーとローカル会計を保持する', async () => {
    const ui = harness({ online: true, fetcher: async () => ({ status: 409, ok: false,
        headers: { get: () => 'application/json' },
        json: async () => ({ code: 'price_changed', differences: [
            { product_id: 1, name: 'パン', displayed_yen: 120, current_yen: 150 } ] }) }) });
    await until(() => ui.database.stores.has('queue'));
    ui.touch();
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    ui.elements['complete-checkout'].fire('click');
    await until(() => [...ui.database.stores.get('queue').values()][0]?.reason === 'price_changed');
    const record = [...ui.database.stores.get('queue').values()][0];
    assert.equal(record.status, 'needs_review');
    assert.equal(record.receipt.displayed_prices[0][1], 120);
    assert.match(ui.elements['recovery-list'].textContent, /150円/);
    assert.match(ui.elements['recovery-list'].textContent, /会計差 30円/);
    ui.events.online();
    await new Promise(resolve => setTimeout(resolve, 15));
    assert.equal(ui.calls.length, 1);
});

test('Guest期限切れは通信障害と区別して再送停止し、別スコープに移さない', async () => {
    const db = new MemoryDB();
    const ui = harness({ database: db, online: true, fetcher: async () => ({ status: 403, ok: false,
        headers: { get: () => 'application/json' },
        json: async () => ({ code: 'guest_expired' }) }) });
    await until(() => db.stores.has('queue'));
    ui.touch();
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    ui.elements['complete-checkout'].fire('click');
    await until(() => [...db.stores.get('queue').values()][0]?.status === 'guest_expired');
    assert.match(ui.elements['sync-state'].textContent, /利用期限が終了/);
    ui.events.online();
    await new Promise(resolve => setTimeout(resolve, 15));
    assert.equal(ui.calls.length, 1);
    const other = harness({ database: db, scope: 'another:session', online: true });
    await until(() => other.elements['sync-state'].textContent === '同期済み');
    assert.equal(other.calls.length, 0);
    assert.equal(db.stores.get('queue').size, 1);
});

test('下書き書込が失敗しても次の書込と会計のキュー保存を妨げない', async () => {
    const db = new MemoryDB();
    const ui = harness({ database: db, online: false });
    await until(() => db.stores.has('drafts'));
    db.failDraft = true;
    ui.touch();
    await until(() => ui.elements['register-message'].textContent.includes('入力中の会計を端末に保持できません'));
    db.failDraft = false;
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    await until(() => db.stores.get('drafts').size === 1);
    ui.elements['complete-checkout'].fire('click');
    await until(() => db.stores.get('queue').size === 1);
    await until(() => ui.quantity.value === 0);
    assert.equal(ui.elements['sync-state'].textContent, '端末に保存済み・サーバー反映未確認 1件（通信後に再送）');
});

test('認証確認後の同一スコープ再読み込みは同じキーを再送し、Guest期限切れは再送しない', async () => {
    const db = new MemoryDB();
    const first = harness({ database: db, online: true, fetcher: async () => ({
        status: 401, ok: false, headers: { get: () => 'application/json' },
        json: async () => ({ code: 'auth_required' }),
    }) });
    await until(() => db.stores.has('queue'));
    first.touch();
    first.elements['received-yen'].value = '500';
    first.elements['received-yen'].fire('input');
    first.elements['complete-checkout'].fire('click');
    await until(() => [...db.stores.get('queue').values()][0]?.status === 'auth_required');
    const key = first.calls[0].submission_key;
    const reload = harness({ database: db, online: true });
    await until(() => [...db.stores.get('queue').values()][0]?.status === 'synced');
    assert.equal(reload.calls.filter(call => call.method !== 'GET').length, 1);
    assert.equal(reload.calls[0].submission_key, key);
});

test('保存状況照会は同じキーと明細が一致した会計だけ保存済みにする', async () => {
    const ui = harness({ online: false, fetcher: async call => {
        assert.equal(call.method, 'GET');
        const local = [...ui.database.stores.get('queue').values()][0].receipt;
        assert.equal(call.url, '/api/register/transactions/' + local.submission_key + '/status');
        return { ok: true, status: 200, headers: { get: () => 'application/json' },
            json: async () => ({ status: 'saved', submission_key: local.submission_key,
                transaction_id: 'server-id', business_date: local.client_business_date,
                kind: local.kind, total_yen: local.total_yen, received_yen: local.received_yen,
                items: local.items, displayed_prices: local.displayed_prices }) };
    } });
    await until(() => ui.database.stores.has('queue'));
    ui.touch();
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    ui.elements['complete-checkout'].fire('click');
    await until(() => ui.database.stores.get('queue').size === 1);
    ui.navigator.onLine = true;
    ui.elements['check-register-status'].fire('click');
    await until(() => [...ui.database.stores.get('queue').values()][0].status === 'synced');
    assert.equal(ui.database.stores.get('queue').size, 1);
    assert.equal(ui.calls.length, 1);
    assert.match(ui.elements['status-check-result'].textContent, /サーバーに保存済み/);
});

test('照会の不一致と未確認は会計キーと端末記録を保持する', async () => {
    for (const mode of ['not_found', 'mismatch', 'failure']) {
        const ui = harness({ online: false, fetcher: async () => ({
            ok: mode !== 'failure', status: mode === 'failure' ? 503 : 200,
            headers: { get: () => 'application/json' },
            json: async () => mode === 'mismatch'
                ? ({ status: 'saved', submission_key: [...ui.database.stores.get('queue').values()][0]
                    .receipt.submission_key, transaction_id: 'server-id', kind: 'sale',
                    business_date: [...ui.database.stores.get('queue').values()][0]
                        .receipt.client_business_date, total_yen: 999, received_yen: 500,
                    items: [[1, 1]], displayed_prices: [[1, 120]] })
                : ({ status: mode, error: '一時的な障害' }),
        }) });
        await until(() => ui.database.stores.has('queue'));
        ui.touch();
        ui.elements['received-yen'].value = '500';
        ui.elements['received-yen'].fire('input');
        ui.elements['complete-checkout'].fire('click');
        await until(() => ui.database.stores.get('queue').size === 1);
        const before = [...ui.database.stores.get('queue').values()][0].receipt.submission_key;
        ui.navigator.onLine = true;
        ui.elements['check-register-status'].fire('click');
        await until(() => ui.calls.length === 1);
        await until(() => !ui.elements['check-register-status'].disabled);
        const after = [...ui.database.stores.get('queue').values()][0];
        assert.equal(after.receipt.submission_key, before);
        assert.equal(ui.database.stores.get('queue').size, 1);
        assert.equal(after.status, mode === 'mismatch' ? 'needs_review' : 'pending');
        if (mode === 'mismatch') { assert.equal(after.reason, 'key_conflict'); }
        assert.equal(ui.calls[0].method, 'GET');
    }
});

test('POSTの200応答でも保存済み明細が違えば同期済みにしない', async () => {
    const ui = harness({ online: true, fetcher: async call => {
        const local = [...ui.database.stores.get('queue').values()][0].receipt;
        return { ok: true, status: 200, headers: { get: () => 'application/json' },
            json: async () => call.method === 'GET'
                ? ({ status: 'saved', submission_key: local.submission_key,
                    transaction_id: 'saved-id', business_date: local.client_business_date,
                    kind: local.kind, total_yen: local.total_yen, received_yen: local.received_yen,
                    items: local.items, displayed_prices: [[1, 121]] })
                : ({ submission_key: local.submission_key, transaction_id: 'saved-id',
                    total_yen: local.total_yen, business_date: local.client_business_date }) };
    } });
    await until(() => ui.database.stores.has('queue'));
    ui.touch();
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    ui.elements['complete-checkout'].fire('click');
    await until(() => [...ui.database.stores.get('queue').values()][0]?.status === 'needs_review');
    const record = [...ui.database.stores.get('queue').values()][0];
    assert.equal(record.reason, 'key_conflict');
    assert.equal(record.receipt.displayed_prices[0][1], 120);
    assert.deepEqual(ui.calls.map(call => call.method || 'POST'), ['POST', 'GET']);
});

test('Adminの旧ログインscopeは同じDatasetの元会計を保持して引き継ぐ', async () => {
    const db = new MemoryDB();
    const old = harness({ database: db, scope: 'dataset:login1', online: false });
    await until(() => db.stores.has('queue'));
    old.touch();
    old.elements['received-yen'].value = '500';
    old.elements['received-yen'].fire('input');
    old.elements['complete-checkout'].fire('click');
    await until(() => db.stores.get('queue').size === 1);
    const before = structuredClone([...db.stores.get('queue').values()][0]);
    const admin = harness({ database: db, scope: 'dataset:admin', online: false });
    await until(() => [...db.stores.get('queue').values()][0].scope === 'dataset:admin');
    const after = [...db.stores.get('queue').values()][0];
    assert.deepEqual(after.receipt, before.receipt);
    assert.equal(after.status, before.status);
    assert.equal(admin.calls.length, 0);
});
