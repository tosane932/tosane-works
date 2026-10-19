const test = require('node:test');
const assert = require('node:assert/strict');
const pager = require('../static/register_pager.js');
const core = require('../static/register_core.js');
for (const [count, pages] of [[0,0],[8,1],[12,1],[13,2],[18,2],[24,2],[30,3],[50,5]]) {
    test(`${count}商品を最大12商品ずつ${pages}ページに分け、順序・IDを保つ`, () => {
        const products = Array.from({ length: count }, (_, i) => ({ id: i + 1, name: `パン${i}`, price: 850 }));
        const grouped = pager.group(products);
        assert.equal(grouped.length, pages);
        assert.ok(grouped.every(page => page.length <= 12));
        assert.deepEqual(grouped.flat(), products);
        if (count > 12) {
            const cart = core.createCart(products);
            core.setQuantity(cart, grouped[0][0].id, 1);
            core.setQuantity(cart, grouped[1][0].id, 2);
            assert.equal(core.calculation(cart, '5000').total, 2550);
            assert.equal(core.calculation(cart, '5000').change, 2450);
        }
    });
}
for (const [count, missing] of [[0,0],[1,1],[6,0],[12,0],[13,1],[18,0],[24,0],[25,1],[50,0]]) {
    test(`${count}商品では末尾だけ${missing}枠を補い、空のグループを増やさない`, () => {
        assert.equal(pager.emptySlots(count), missing);
        assert.ok(missing >= 0 && missing < 2);
        if (count) { assert.equal((count + missing) % 2, 0); }
    });
}

for (const rows of [2,3,4]) {
    test(`${rows}行では不足分だけを1列まで補い、実商品順を変えない`, () => {
        for (let count = 0; count <= 50; count++) {
            const missing = pager.emptySlots(count, rows);
            assert.ok(missing >= 0 && missing < rows);
            assert.equal((count + missing) % rows, 0);
        }
    });
}
