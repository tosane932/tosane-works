const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname, '../static', name), 'utf8');

function storage() {
    const stores = new Map();
    const db = {
        objectStoreNames: { contains: name => stores.has(name) },
        createObjectStore(name) { stores.set(name, new Map()); return { createIndex() {} }; },
        close() {},
        transaction(name) {
            const tx = { oncomplete: null, onerror: null, onabort: null };
            tx.objectStore = () => ({
                put(value) { const req = { onsuccess: null, result: value };
                    queueMicrotask(() => { stores.get(name).set(value.id, structuredClone(value));
                        req.onsuccess?.(); setImmediate(() => tx.oncomplete?.()); });
                    return req; },
                get(id) { const req = { onsuccess: null, result: null };
                    queueMicrotask(() => { req.result = stores.get(name).get(id);
                        req.onsuccess?.(); setImmediate(() => tx.oncomplete?.()); });
                    return req; },
            });
            return tx;
        },
    };
    return { stores, indexedDB: { open() { const req = { result: db };
        queueMicrotask(() => { req.onupgradeneeded?.(); req.onsuccess?.(); }); return req; } } };
}

test('PIN暗号化の商品版と許可をIndexedDBから再起動後に復元し、誤PINを拒否', async () => {
    const memory = storage();
    const globals = { indexedDB: memory.indexedDB, crypto: webcrypto,
        TextEncoder, TextDecoder, Uint8Array, atob, btoa, Error, JSON,
        RegisterOfflineStore: null };
    vm.runInNewContext(source('register_offline_store.js'), globals);
    const catalog = { dataset_id: 'admin', products: [{ id: 1, name: 'パン', price: 120 }],
        token: 'signed-permit', business_date: '2026-10-16' };
    await globals.RegisterOfflineStore.save(catalog, 'secure-pin-123');
    const ciphertext = memory.stores.get('offline').get('active');
    assert.ok(!JSON.stringify(ciphertext).includes('signed-permit'));
    globals.RegisterOfflineStore = null; // another page load, same storage
    vm.runInNewContext(source('register_offline_store.js'), globals);
    assert.equal(JSON.stringify(await globals.RegisterOfflineStore.load('secure-pin-123')),
        JSON.stringify(catalog));
    await assert.rejects(globals.RegisterOfflineStore.load('wrong-pin'), /PIN/);
});

test('Service Workerは専用シェルと資産だけをキャッシュし、APIと認証済みpreviewは除外', async () => {
    const events = {};
    const items = new Map();
    const cache = {
        async addAll(paths) { paths.forEach(p => items.set(p, { path: p })); },
        async put(path, result) { items.set(path, result); },
    };
    const self = { location: { origin: 'https://bakery.test' },
        clients: { claim: async () => {} },
        addEventListener: (name, callback) => { events[name] = callback; } };
    const shell = { ok: true, redirected: false,
        headers: { get: key => key === 'X-Register-Offline-Shell' ? '1' : null } };
    vm.runInNewContext(source('register_offline_sw.js'), {
        self, URL, Promise, caches: { open: async () => cache,
            match: async path => items.get(path) }, fetch: async () => shell,
    });
    let installation;
    events.install({ waitUntil: promise => { installation = promise; } });
    await installation;
    assert.ok(items.has('/register/offline'));
    assert.ok(items.has('/static/register_offline_boot.js'));
    assert.ok(!items.has('/register/preview'));
    assert.ok(!items.has('/api/register/offline/session'));
    let handled = false;
    events.fetch({ request: { method: 'GET', url: 'https://bakery.test/api/register/offline/session',
        mode: 'same-origin' }, respondWith: () => { handled = true; } });
    assert.equal(handled, false);
    let fallback;
    events.fetch({ request: { method: 'GET', url: 'https://bakery.test/register/preview',
        mode: 'navigate' }, respondWith: promise => { fallback = promise; } });
    assert.equal(await fallback, shell); // network-first; connection loss uses the cached shell.
});

test('通信断の再起動シェルはPINとDataset・期限を検査して商品版を復元する', async () => {
    const core = require('../static/register_core.js');
    const today = core.tokyoDay(new Date());
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) { nodes.set(id, {
            id, children: [], hidden: false, textContent: '', dataset: {},
            addEventListener(type, listener) { this[type] = listener; },
            setAttribute() {}, append(...children) { this.children.push(...children); },
            replaceChildren(...children) { this.children = children; },
        }); }
        return nodes.get(id);
    };
    const prepared = { dataset_id: 'dataset-1', scope: 'dataset-1:admin',
        grant_id: 'grant', device_id: 'device', token: 'token', catalog_proof: 'proof',
        business_date: today, next_business_date: null,
        issued_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 3600000).toISOString(),
        products: [{ id: 7, name: 'パン', price: 120, image: null, is_active: true }] };
    node('offline-pin').value = 'secure-pin';
    const scripts = [];
    const document = { getElementById: node, createElement: () => node(`node-${nodes.size}`),
        querySelector: () => node('csrf'), head: { append(script) { scripts.push(script.src); } } };
    const listeners = {};
    const globals = { document, RegisterOfflineStore: { load: async () => prepared },
        RegisterCore: core, navigator: { onLine: false }, localStorage: { getItem: () => null },
        window: { addEventListener: (type, callback) => { listeners[type] = callback; },
                  dispatchEvent() {} }, Date, Event, AbortController, setTimeout, clearTimeout,
        fetch: async () => { throw Error('must not fetch offline'); }, Error, JSON };
    vm.runInNewContext(source('register_offline_boot.js'), globals);
    await node('offline-unlock').submit({ preventDefault() {} });
    assert.equal(node('offline-unlock').hidden, true);
    assert.equal(node('product-grid').children.length, 1);
    assert.equal(JSON.parse(node('register-config').textContent).products[0].id, 7);
    assert.deepEqual(scripts, ['/static/register.js']);
    assert.equal(globals.RegisterOfflineAuth, 'offline');
});

test('PIN解除直後の通信復旧イベントを取り逃がさず、古い通信結果で認証状態を戻さない', async () => {
    const core = require('../static/register_core.js');
    const nodes = new Map();
    const node = id => {
        if (!nodes.has(id)) { nodes.set(id, {
            id, children: [], hidden: false, textContent: '', dataset: {},
            addEventListener(type, listener) { this[type] = listener; },
            setAttribute() {}, append(...children) { this.children.push(...children); },
            replaceChildren(...children) { this.children = children; },
        }); }
        return nodes.get(id);
    };
    const day = core.tokyoDay(new Date());
    const prepared = { dataset_id: 'dataset-1', scope: 'dataset-1:admin',
        grant_id: 'grant', device_id: 'device', token: 'token', catalog_proof: 'proof',
        business_date: day, next_business_date: null,
        issued_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 3600000).toISOString(),
        products: [{ id: 7, name: 'パン', price: 120, image: null, is_active: true }] };
    node('offline-pin').value = 'secure-pin';
    let finishFirst;
    let calls = 0;
    const listeners = {};
    const globals = { document: {
        getElementById: node, createElement: () => node(`node-${nodes.size}`),
        querySelector: () => node('csrf'), head: { append() {} },
    }, RegisterOfflineStore: { load: async () => prepared }, RegisterCore: core,
    navigator: { onLine: true }, localStorage: { getItem: () => null },
    window: { addEventListener: (type, callback) => { listeners[type] = callback; },
              dispatchEvent() {} }, Date, Event, AbortController, setTimeout, clearTimeout,
    fetch: async () => {
        calls++;
        if (calls === 1) { return new Promise(resolve => { finishFirst = resolve; }); }
        return { ok: true, status: 200, redirected: false,
                 json: async () => ({ dataset_id: 'dataset-1', csrf_token: 'fresh' }) };
    }, Error, JSON };
    vm.runInNewContext(source('register_offline_boot.js'), globals);
    await node('offline-unlock').submit({ preventDefault() {} });
    assert.equal(typeof listeners.online, 'function');
    await listeners.online();
    assert.equal(globals.RegisterOfflineAuth, 'ready');
    finishFirst({ ok: false, status: 503, redirected: false });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(globals.RegisterOfflineAuth, 'ready');
    assert.equal(node('csrf').content, 'fresh');
});
