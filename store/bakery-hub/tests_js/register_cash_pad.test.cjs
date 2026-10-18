const test = require('node:test');
const assert = require('node:assert/strict');
const pad = require('../static/register_cash_pad.js');
const core = require('../static/register_core.js');

test('数字・00・末尾削除・クリアを使い、先頭ゼロを正規化する', () => {
    let value = '';
    for (const key of ['0', '00', '5', '00', '0']) { value = pad.edit(value, key); }
    assert.equal(value, '5000');
    assert.equal(pad.edit(value, 'backspace'), '500');
    assert.equal(pad.edit(value, 'clear'), '');
    assert.equal(pad.edit('', 'backspace'), '');
    assert.equal(pad.edit('1', 'backspace'), '');
});
test('上限超過・文字・符号・小数は金額を変えずに拒否する', () => {
    assert.equal(pad.edit('100000000', '0'), '1000000000');
    for (const key of ['0', '00', '-1', '.', 'NaN', '1e2', 'paste']) {
        assert.equal(pad.edit('1000000000', key), '1000000000');
    }
    assert.equal(pad.edit('999999999', '9'), '999999999');
    assert.equal(pad.edit('壊れた下書き', 'backspace'), '');
});
test('入力の修正が既存のお釣り計算・会計内容に反映される', () => {
    const cart = core.createCart([{id:1,name:'パン',price:280}]);
    core.setQuantity(cart, 1, 2);
    let value = ['1','0','0','0'].reduce(pad.edit, '');
    assert.equal(core.calculation(cart, value).change, 440);
    value = pad.edit(value, 'backspace');
    assert.equal(core.calculation(cart, value).valid, false);
    value = pad.edit(value, '0');
    assert.equal(core.calculation(cart, value).change, 440);
    assert.deepEqual([...cart.quantities], [[1,2]]);
});
