const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../static/register_core.js');

const products = [
    { id: 1, name: 'クロワッサン', price: 120 },
    { id: 2, name: 'メロンパン', price: 140 },
    { id: 3, name: '無料品', price: 0 },
    { id: 4, name: '上限価格', price: 500 },
];
const fresh = () => core.createCart(products);

test('タッチと数量の直接入力は端末内で完結し、±50を守る', () => {
    const cart = fresh();
    core.setQuantity(cart, 1, 1);
    core.setQuantity(cart, 1, 2);
    assert.equal(cart.quantities.get(1), 2);
    core.setQuantity(cart, 4, '50');
    assert.equal(core.calculation(cart, '25500').total, 25240);
    for (const value of [51, -51, 1.5, true, 'oops']) {
        assert.throws(() => core.setQuantity(cart, 1, value));
    }
    core.setQuantity(cart, 1, 0);
    assert.equal(cart.quantities.has(1), false);
});

test('返金に入る直前に確認を要求し、販売との混在を拒否', () => {
    const cart = fresh();
    assert.throws(() => core.setQuantity(cart, 1, -1), { code: 'refund_confirmation' });
    core.setQuantity(cart, 1, -1, true);
    assert.equal(core.calculation(cart, '').total, -120);
    assert.throws(() => core.setQuantity(cart, 2, 1), /別会計/);
    core.setQuantity(cart, 1, -50);
    assert.equal(core.calculation(cart, '').total, -6000);
});

test('販売の合計・お釣りと不正なお預かり、0円会計', () => {
    const cart = fresh();
    core.setQuantity(cart, 1, 2);
    core.setQuantity(cart, 2, 1);
    assert.deepEqual(
        [core.calculation(cart, '500').total, core.calculation(cart, '500').change],
        [380, 120]
    );
    for (const amount of ['379', '-1', '50.5', 'abc', '1000000001']) {
        assert.equal(core.calculation(cart, amount).valid, false);
    }
    core.setQuantity(cart, 1, 0);
    core.setQuantity(cart, 2, 0);
    core.setQuantity(cart, 3, 1);
    assert.equal(core.calculation(cart, '0').valid, false);
});

test('会計スナップショット・東京の営業日・送信キーを固定する', () => {
    const cart = fresh();
    core.setQuantity(cart, 2, 1);
    const key = '45ad5f47-18ab-4561-ac9d-3d3b068f7ac8';
    const receipt = core.receipt(cart, '500', key, new Date('2026-10-15T16:00:00Z'));
    assert.deepEqual(receipt.items, [[2, 1]]);
    assert.deepEqual(receipt.displayed_prices, [[2, 140]]);
    assert.equal(receipt.client_business_date, '2026-10-16');
    assert.equal(receipt.completed_at_local, '2026-10-15T16:00:00.000Z');
    assert.equal(receipt.submission_key, key);
    assert.equal(receipt.snapshots[0].unit_price_yen, 140);
    products[1].price = 150;
    assert.equal(receipt.snapshots[0].unit_price_yen, 140);
    products[1].price = 140;
});
