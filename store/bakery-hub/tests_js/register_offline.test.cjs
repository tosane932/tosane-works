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
    const catalog = { dataset_id: 'admin', products: [{ id: 1, name: 'パン', price: 120, image: 'data:image/webp;base64,' + 'A'.repeat(600000) }],
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
    assert.ok(items.has('/static/register_offline_boot.js?v=adaptive-5'));
    assert.ok(items.has('/static/register_layout.js?v=adaptive-5'));
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
        products: [{ id: 7, name: 'パン', price: 100000, image: null, is_active: true }] };
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
    const tile=node('product-grid').children[0];
    assert.deepEqual(Array.from(tile.children, child=>child.className), ['remove-one','add-one','quantity-label']);
    assert.equal(tile.children[0].children[0].textContent,'−');
    assert.ok(tile.children[1].children.some(child=>child.className==='product-price product-price-long'));
    assert.equal(tile.children[2].children[0].className,'quantity-caption');
    assert.equal(JSON.parse(node('register-config').textContent).products[0].id, 7);
    assert.deepEqual(scripts, ['/static/register.js?v=adaptive-5']);
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
    }, RegisterOfflineStore: { load: async () => prepared }, RegisterCore: core, RegisterPager: { mount() {} },
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

test('旧キャッシュを残してもv5のシェル・表示制御・キーパッド資産だけを復元する', async () => {
    const events = {};
    const wanted = new Map([
        ['/register/offline', 'v5-shell'],
        ['https://bakery.test/static/register_cash_pad.js?v=adaptive-5', 'v5-keypad'],
    ]);
    const opened = [];
    vm.runInNewContext(source('register_offline_sw.js'), {
        self: { location:{origin:'https://bakery.test'}, addEventListener:(name,fn)=>events[name]=fn },
        URL, Promise,
        caches: { open:async version => {
            opened.push(version);
            return {match:async request=>wanted.get(typeof request==='string' ? request : request.url)};
        }, match:()=>{throw Error('must not search old cache versions');} },
        fetch:async()=>{throw Error('offline');},
    });
    for(const [url,mode,expected] of [
        ['https://bakery.test/register/preview','navigate','v5-shell'],
        ['https://bakery.test/static/register_cash_pad.js?v=adaptive-5','same-origin','v5-keypad'],
    ]) {
        let response;
        events.fetch({request:{method:'GET',url,mode},respondWith:promise=>response=promise});
        assert.equal(await response,expected);
    }
    assert.ok(opened.every(version=>version==='register-offline-v5-adaptive'));
});

test('旧Workerが有効で更新待ちなら準備完了とせず、PIN商品版・元会計を保持する', async () => {
    const nodes=new Map();
    const node=id=>{
        if(!nodes.has(id)) { nodes.set(id,{value:'',textContent:'',disabled:false,
            addEventListener(type,handler){this[type]=handler;},querySelector:()=>node('button'),
            replaceChildren(){throw Error('must not display ready link');}}); }
        return nodes.get(id);
    };
    node('register-config').textContent=JSON.stringify({scope:'dataset:admin',offlinePrepareUrl:'/prepare'});
    node('offline-setup-pin').value='long-enough-pin';
    let saves=0, updates=0;
    const registration={active:{postMessage(){throw Error('must not cache through old worker');}},
        waiting:{},update:async()=>{updates++;}};
    vm.runInNewContext(source('register_offline_setup.js'),{
        document:{getElementById:node,querySelector:()=>({content:'csrf'})},
        RegisterOfflineStore:{save:async()=>{saves++;}},
        navigator:{onLine:true,serviceWorker:{register:async()=>registration,ready:Promise.resolve(registration)}},
        crypto:{subtle:{},randomUUID:()=> 'device'}, indexedDB:{}, caches:{},
        fetch:async()=>({ok:true,headers:{get:()=> 'application/json'},json:async()=>({scope:'dataset:admin',token:'permit'})}),
        Error,Promise,
    });
    await node('offline-setup').submit({preventDefault(){}});
    assert.equal(updates,1);
    assert.equal(saves,0);
    assert.match(node('offline-setup-state').textContent,/すべてのレジタブを閉じて開き直し/);
    assert.match(node('offline-setup-state').textContent,/準備は完了していません/);
    assert.equal(node('button').disabled,false);
});
