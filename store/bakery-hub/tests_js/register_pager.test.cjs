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
