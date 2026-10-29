const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(editing = false) {
  const listeners = {};
  const made = {name: 'made_quantity', value: editing ? '5' : ''};
  const discarded = {name: 'discarded_quantity', value: editing ? '1' : ''};
  const note = {name: 'note', value: ''};
  const product = {name: 'product_key', value: 'register:1'};
  const confirmation = {name: 'overage_confirm', checked: false, focus() { this.focused = true; }};
  const status = {textContent: editing ? '保存済み' : '未入力'};
  const caution = {hidden: true};
  const button = {disabled: false};
  const items = {made_quantity: made, discarded_quantity: discarded, note, product_key: product,
                 overage_confirm: confirmation};
  const form = {
    dataset: {savedMade: '5', savedDiscarded: '1', savedNote: ''},
    hasAttribute: name => editing && name === 'data-waste-edit',
    elements: {namedItem: name => items[name]},
    querySelector: selector => selector === '[data-save-state]' ? status :
      selector === '[data-waste-caution]' ? caution : button,
    querySelectorAll: () => editing ? [made, discarded, note] : [made, discarded, note, product],
    addEventListener: (name, callback) => { listeners[name] = callback; },
    checkValidity: () => true,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'static', 'waste_records.js'), 'utf8'),
                     {document: {querySelectorAll: () => [form]}});
  const change = (field, value) => { field.value = value; listeners.input({target: field}); };
  const submit = () => { let blocked = false; listeners.submit({preventDefault() { blocked = true; }}); return blocked; };
  return {made, discarded, note, confirmation, status, caution, button, change, submit};
}

test('overage needs explicit confirmation and a changed quantity resets it', () => {
  const view = setup();
  view.change(view.made, '0');
  view.change(view.discarded, '2');
  assert.equal(view.caution.hidden, false);
  assert.equal(view.submit(), true);
  assert.match(view.status.textContent, /確認欄/);
  view.confirmation.checked = true;
  view.change(view.discarded, '3');
  assert.equal(view.confirmation.checked, false);
  view.confirmation.checked = true;
  assert.equal(view.submit(), false);
  assert.equal(view.button.disabled, true);
  assert.equal(view.status.textContent, '保存中…');
});

test('unchanged edit avoids submission; changed edit may submit', () => {
  const view = setup(true);
  assert.equal(view.submit(), true);
  assert.equal(view.status.textContent, '変更はありません');
  view.change(view.discarded, '2');
  assert.equal(view.status.textContent, '未保存の変更があります');
  assert.equal(view.submit(), false);
  assert.equal(view.button.disabled, true);
});
