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
        'complete-checkout', 'received-yen', 'sync-state', 'product-grid',
        'cart-lines', 'checkout-kind', 'total-label', 'total-yen', 'cash-area',
        'jump-checkout', 'checkout-title',
        'change-yen', 'cash-hint', 'refund-dialog', 'refund-dialog-detail',
        'refund-cancel', 'refund-confirm', 'register-back', 'discard-dialog',
        'discard-cancel', 'discard-confirm'];
    const elements = Object.fromEntries(ids.map(id => [id, element(id)]));
    elements['register-config'].textContent = JSON.stringify({
        products, scope, saveEnabled: true, postUrl: '/api/register/transactions',
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
        calls.push(JSON.parse(options.body));
        if (fetcher) { return fetcher(calls.at(-1)); }
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
    await until(() => ui.elements['sync-state'].textContent === '未同期 1件');
    assert.equal(ui.elements['product-grid'].children[0].querySelector().value, 0);
    assert.equal(ui.calls.length, 0);
    ui.navigator.onLine = true;
    ui.events.online();
    await until(() => [...ui.database.stores.get('queue').values()][0].status === 'synced');
    assert.equal(ui.calls.length, 1);
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
    assert.equal(ui.elements['sync-state'].textContent, '同期確認が必要です 1件 / 未同期 0件');
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
    await until(() => ui.elements['sync-state'].textContent === '未同期 1件');
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
    assert.equal(ui.calls.length, 2);
    assert.equal(ui.calls[0].submission_key, ui.calls[1].submission_key);
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
    await until(() => [...ui.database.stores.get('queue').values()][0]?.status === 'needs_review');
    assert.equal(ui.database.stores.get('queue').size, 1);
});
