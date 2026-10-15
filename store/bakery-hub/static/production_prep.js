"use strict";
(() => {
  const recipeRows = document.querySelector("#prep-ingredient-rows");
  const add = document.querySelector("#prep-add-ingredient");
  if (recipeRows && add) {
    add.addEventListener("click", () => {
      if (recipeRows.children.length >= 30) return;
      const row = recipeRows.firstElementChild.cloneNode(true);
      row.querySelectorAll("input").forEach(input => { input.value = ""; });
      row.querySelector("select").value = "g";
      recipeRows.append(row);
      row.querySelector("input").focus();
    });
    recipeRows.addEventListener("click", event => {
      if (event.target.matches(".prep-remove-ingredient") && recipeRows.children.length > 1) {
        event.target.closest(".prep-ingredient-row").remove();
      }
    });
  }

  const cards = [...document.querySelectorAll(".prep-plan-card")];
  const form = document.querySelector("#prep-bulk-form");
  if (!cards.length || !form) return;
  const materialRoot = document.querySelector("#prep-materials");
  const stockRoot = document.querySelector("#prep-stock-list");
  const breakdownRoot = document.querySelector("#prep-breakdown-content");
  const empty = materialRoot.querySelector(".prep-empty");
  const error = document.querySelector("#prep-input-error");
  const unsaved = document.querySelector("#prep-unsaved");
  const save = form.querySelector(".prep-save-all");
  const MAX = 1000;
  const keyFor = (name, unit) => JSON.stringify([name.trim(), unit]);
  const format = micro => {
    const whole = micro / 1000000n;
    const fractional = (micro % 1000000n).toString().padStart(6, "0").replace(/0+$/, "");
    return whole.toString() + (fractional ? "." + fractional : "");
  };
  const parseStock = value => {
    if (value === "") return null;
    if (!/^(?:0|[1-9]\d{0,8})(?:\.\d{1,6})?$/.test(value)) return undefined;
    const [whole, decimal = ""] = value.split(".");
    return BigInt(whole) * 1000000n + BigInt(decimal.padEnd(6, "0") || "0");
  };
  // 行を再作成せず、在庫入力中のフォーカス・値・開閉状態を保持する。
  const materialRows = new Map();
  materialRoot.querySelectorAll(".prep-material").forEach(row => materialRows.set(keyFor(row.dataset.name, row.dataset.unit), row));
  const stockRows = new Map();
  stockRoot.querySelectorAll(".prep-stock-item").forEach(row => stockRows.set(keyFor(row.dataset.name, row.dataset.unit), row));

  function updateStock(row, amount, unit) {
    const input = row.querySelector(".prep-stock");
    const state = row.querySelector(".prep-stock-status");
    const stock = parseStock(input.value);
    state.classList.toggle("invalid", stock === undefined);
    state.classList.toggle("shortage", stock !== undefined && stock !== null && stock < amount);
    if (stock === undefined) state.textContent = "現在量の入力が正しくありません（0以上、小数第6位まで）。";
    else if (stock === null) state.textContent = "未確認";
    else if (stock < amount) state.textContent = `不足 ${format(amount - stock)}${unit}`;
    else state.textContent = `余裕 ${format(stock - amount)}${unit}`;
  }
  function makeMaterial(item) {
    const row = document.createElement("div");
    row.className = "prep-material";
    row.dataset.name = item.name; row.dataset.unit = item.unit;
    const name = document.createElement("strong"); name.textContent = item.name;
    const amount = document.createElement("span");
    amount.append("必要 ", document.createElement("output"), item.unit);
    row.append(name, amount);
    return row;
  }
  function makeStock(item) {
    const row = document.createElement("div");
    row.className = "prep-stock-item";
    row.dataset.name = item.name; row.dataset.unit = item.unit;
    const name = document.createElement("strong"); name.textContent = item.name;
    const amount = document.createElement("span");
    amount.append("必要量：", document.createElement("output"), item.unit);
    const label = document.createElement("label"); label.textContent = "現在量：";
    const input = document.createElement("input");
    input.type = "text"; input.inputMode = "decimal"; input.className = "prep-stock"; input.placeholder = "未入力";
    label.append(input, item.unit);
    const state = document.createElement("p"); state.className = "prep-stock-status";
    row.append(name, amount, label, state);
    input.addEventListener("input", () => updateStock(row, BigInt(row.dataset.amount), item.unit));
    return row;
  }
  function recalculate() {
    const totals = new Map();
    let invalid = false;
    let changed = false;
    let chosen = 0;
    cards.forEach(card => {
      const selected = form.querySelector(`[data-choose="${card.dataset.recipeId}"]`).checked;
      const number = card.querySelector('input[type="number"]');
      card.hidden = !selected;
      number.disabled = !selected;
      changed ||= selected !== (card.dataset.hasPlan === "true");
      if (!selected) return;
      chosen++;
      const raw = number.value;
      if (!/^(?:0|[1-9]\d{0,3})$/.test(raw) || Number(raw) > MAX) {
        invalid = true;
        return;
      }
      const count = Number(raw);
      changed ||= count !== Number(card.dataset.saved);
      const base = BigInt(card.dataset.base);
      card.querySelectorAll(".prep-ingredients-data li").forEach(ingredient => {
        const name = ingredient.dataset.name.trim();
        const unit = ingredient.dataset.unit;
        const numerator = BigInt(ingredient.dataset.milli) * BigInt(count) * 1000n;
        const amount = (numerator + base - 1n) / base;
        const key = keyFor(name, unit);
        if (!totals.has(key)) totals.set(key, { name, unit, amount: 0n, parts: [] });
        const entry = totals.get(key);
        entry.amount += amount;
        if (amount) entry.parts.push([card.querySelector("h3").textContent.trim(), amount]);
      });
    });
    error.hidden = !invalid;
    if (invalid) error.textContent = "製造予定数は0～1000の整数で入力してください。";
    unsaved.hidden = !changed;
    save.disabled = invalid || chosen === 0;
    if (invalid) return;
    const entries = [...totals.values()].filter(item => item.amount > 0n)
      .sort((a, b) => a.name.localeCompare(b.name, "ja") || a.unit.localeCompare(b.unit, "ja"));
    const active = new Set(entries.map(item => keyFor(item.name, item.unit)));
    materialRows.forEach((row, key) => { row.hidden = !active.has(key); });
    stockRows.forEach((row, key) => { row.hidden = !active.has(key); });
    empty.hidden = entries.length > 0;
    breakdownRoot.replaceChildren();
    entries.forEach(item => {
      const key = keyFor(item.name, item.unit);
      let row = materialRows.get(key);
      if (!row) { row = makeMaterial(item); materialRows.set(key, row); materialRoot.insertBefore(row, empty); }
      row.hidden = false;
      row.querySelector("output").textContent = format(item.amount);
      let stock = stockRows.get(key);
      if (!stock) { stock = makeStock(item); stockRows.set(key, stock); stockRoot.append(stock); }
      stock.hidden = false;
      stock.dataset.amount = String(item.amount);
      stock.querySelector("output").textContent = format(item.amount);
      updateStock(stock, item.amount, item.unit);
      const part = document.createElement("div");
      part.className = "prep-breakdown-item";
      const title = document.createElement("strong"); title.textContent = `${item.name}（${item.unit}）`;
      const list = document.createElement("ul");
      item.parts.forEach(([product, amount]) => {
        const li = document.createElement("li");
        li.textContent = `${product}：${format(amount)}${item.unit}`;
        list.append(li);
      });
      part.append(title, list);
      breakdownRoot.append(part);
    });
  }
  form.addEventListener("submit", event => {
    recalculate();
    if (save.disabled) {
      event.preventDefault();
      error.hidden = false;
      error.textContent = "パンを選び、0～1000の整数で個数を入力してください。";
    }
  });
  form.querySelectorAll("[data-choose]").forEach(choice => choice.addEventListener("change", recalculate));
  cards.forEach(card => {
    const number = card.querySelector('input[type="number"]');
    const range = card.querySelector('input[type="range"]');
    const syncRange = () => {
      if (!/^(?:0|[1-9]\d{0,3})$/.test(number.value) || Number(number.value) > MAX) return;
      const value = Number(number.value);
      range.max = String(Math.min(MAX, Math.max(100, Math.ceil(value / 100) * 100)));
      range.value = String(value);
      range.setAttribute("aria-label", `${card.querySelector("h3").textContent}の製造予定数（0～${range.max}個）`);
    };
    number.addEventListener("input", () => { syncRange(); recalculate(); });
    range.addEventListener("input", () => { number.value = range.value; recalculate(); });
    card.querySelectorAll("[data-step]").forEach(button => button.addEventListener("click", () => {
      const current = Number(number.value);
      number.value = String(Math.max(0, Math.min(MAX, (Number.isInteger(current) && current >= 0 && current <= MAX ? current : 0) + Number(button.dataset.step))));
      syncRange(); recalculate();
    }));
    syncRange();
  });
  recalculate();
})();
