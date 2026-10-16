const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const core = require('../static/register_core.js');
const source = fs.readFileSync(path.join(__dirname, '../static/register_demo.js'), 'utf8');

function element() {
    const listeners = {};
    return {
        hidden: false, disabled: false, value: '', textContent: '', children: [],
        classList: { toggle() {} },
        addEventListener(type, handler) { listeners[type] = handler; },
        fire(type, event = {}) { listeners[type]?.(event); },
        append(...items) { this.children.push(...items); },
        replaceChildren() { this.children = []; },
        showModal() { this.open = true; }, close() { this.open = false; },
        scrollIntoView() {},
        querySelectorAll() { return []; },
    };
}
function harness() {
    const ids = ['register-root', 'register-config', 'product-grid', 'received-yen',
        'complete-checkout', 'demo-next', 'demo-result', 'register-message', 'cart-lines',
        'checkout-kind', 'total-label', 'total-yen', 'jump-checkout', 'cash-area',
        'change-yen', 'cash-hint', 'refund-dialog', 'refund-dialog-detail',
        'refund-cancel', 'refund-confirm', 'register-back', 'discard-dialog',
        'discard-cancel', 'discard-confirm', 'catalog-title', 'checkout-title'];
    const elements = Object.fromEntries(ids.map(id => [id, element()]));
    elements['register-config'].textContent = JSON.stringify({ demo: true, products: [
        { id: 1, name: 'パン', price: 120, image: '/static/register_images/one.webp' },
    ] });
    const quantity = element();
    const plus = element();
    plus.dataset = { action: 'plus' };
    const minus = element();
    minus.dataset = { action: 'minus' };
    const tile = { dataset: { productId: '1' }, classList: { toggle() {} },
        querySelector: () => quantity, querySelectorAll: () => [plus, minus] };
    plus.closest = selector => selector === 'button[data-action]' ? plus : tile;
    minus.closest = selector => selector === 'button[data-action]' ? minus : tile;
    quantity.matches = selector => selector === '.quantity-input';
    quantity.closest = () => tile;
    const image = { hidden: false, complete: false, naturalWidth: 1,
        nextElementSibling: { hidden: true }, addEventListener(type, handler) {
            if (type === 'error') { this.fail = handler; }
        } };
    elements['product-grid'].children = [tile];
    elements['product-grid'].querySelectorAll = selector => selector === 'img' ? [image] : [];
    elements['register-back'].href = '/home';
    const document = { getElementById: id => elements[id], createElement: element };
    let calls = 0;
    const forbidden = () => { calls++; throw Error('demo attempted persistence or HTTP'); };
    vm.runInNewContext(source, { document, RegisterCore: core, Intl,
        get indexedDB() { return forbidden(); }, get fetch() { return forbidden(); },
        get sessionStorage() { return forbidden(); }, location: { assign: forbidden },
    }, { filename: 'register_demo.js' });
    return { elements, image, quantity, plus, minus, get calls() { return calls; },
        click(button) { elements['product-grid'].fire('click', { target: button }); } };
}

test('商品・数量・預かり・お釣り・疑似完了・次の会計は通信と端末保存なし', () => {
    const ui = harness();
    ui.click(ui.plus);
    ui.quantity.value = '3';
    ui.elements['product-grid'].fire('change', { target: ui.quantity });
    assert.equal(ui.quantity.value, 3);
    assert.equal(ui.elements['total-yen'].textContent, '360円');
    ui.elements['received-yen'].value = '500';
    ui.elements['received-yen'].fire('input');
    assert.equal(ui.elements['change-yen'].textContent, '140円');
    assert.equal(ui.elements['complete-checkout'].disabled, false);
    ui.elements['complete-checkout'].fire('click');
    ui.elements['complete-checkout'].fire('click');
    assert.equal(ui.elements['demo-result'].textContent,
        '練習会計が完了しました。この会計は保存されていません。');
    assert.equal(ui.elements['complete-checkout'].disabled, true);
    ui.click(ui.plus);
    assert.equal(ui.quantity.value, 3);
    ui.elements['demo-next'].fire('click');
    assert.equal(ui.quantity.value, 0);
    assert.equal(ui.elements['received-yen'].value, '');
    assert.equal(ui.elements['demo-result'].hidden, true);
    assert.equal(ui.calls, 0);
});

test('返金確認と画像欠損の文字タイル', () => {
    const ui = harness();
    ui.click(ui.minus);
    assert.equal(ui.elements['refund-dialog'].open, true);
    assert.equal(ui.quantity.value, 0);
    ui.elements['refund-cancel'].fire('click');
    assert.equal(ui.quantity.value, 0);
    ui.quantity.value = '-2';
    ui.elements['product-grid'].fire('change', { target: ui.quantity });
    ui.elements['refund-confirm'].fire('click');
    assert.equal(ui.quantity.value, -2);
    assert.equal(ui.elements['total-yen'].textContent, '240円');
    assert.equal(ui.elements['cash-area'].hidden, true);
    ui.elements['complete-checkout'].fire('click');
    assert.equal(ui.elements['demo-result'].hidden, false);
    ui.image.fail();
    assert.equal(ui.image.hidden, true);
    assert.equal(ui.image.nextElementSibling.hidden, false);
    assert.equal(ui.calls, 0);
});
