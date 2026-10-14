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
  const materialRoot = document.querySelector("#prep-materials");
  if (!cards.length || !materialRoot) return;
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
  const storedStocks = new Map();
  materialRoot.querySelectorAll(".prep-material").forEach(row => {
    storedStocks.set(keyFor(row.dataset.name, row.dataset.unit), row.querySelector(".prep-stock").value);
  });
  const error = document.querySelector("#prep-input-error");
  const unsaved = document.querySelector("#prep-unsaved");
  function recalculate() {
    const totals = new Map();
    let invalidCount = false;
    let changed = false;
    cards.forEach(card => {
      const raw = card.querySelector('input[name="quantity"]').value;
      if (!/^(?:0|[1-9]\d{0,3})$/.test(raw) || Number(raw) > MAX) {
        invalidCount = true;
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
    error.hidden = !invalidCount;
    if (invalidCount) error.textContent = "製造予定数は0～1000の整数で入力してください。";
    unsaved.hidden = !changed;
    if (invalidCount) return;
    materialRoot.replaceChildren();
    const entries = [...totals.values()].filter(item => item.amount > 0n).sort((a, b) => a.name.localeCompare(b.name, "ja") || a.unit.localeCompare(b.unit, "ja"));
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "prep-empty";
      empty.textContent = "予定数を設定すると、ここに必要材料が表示されます。";
      materialRoot.append(empty);
      return;
    }
    entries.forEach(item => {
      const row = document.createElement("article");
      row.className = "prep-material";
      const head = document.createElement("div");
      head.className = "prep-material-head";
      const name = document.createElement("strong"); name.textContent = item.name;
      const needed = document.createElement("span"); needed.textContent = `必要 ${format(item.amount)}${item.unit}`;
      head.append(name, needed);
      const label = document.createElement("label"); label.textContent = `現在量（${item.unit}）`;
      const input = document.createElement("input"); input.type = "text"; input.inputMode = "decimal"; input.className = "prep-stock"; input.placeholder = "未入力";
      const key = keyFor(item.name, item.unit);
      input.value = storedStocks.get(key) || "";
      label.append(input);
      const state = document.createElement("p"); state.className = "prep-stock-status";
      const details = document.createElement("details");
      const summary = document.createElement("summary"); summary.textContent = "商品の内訳";
      const list = document.createElement("ul");
      item.parts.forEach(([product, amount]) => {
        const li = document.createElement("li"); li.textContent = `${product}：${format(amount)}${item.unit}`; list.append(li);
      });
      details.append(summary, list);
      const updateStock = () => {
        storedStocks.set(key, input.value);
        const stock = parseStock(input.value);
        state.classList.toggle("invalid", stock === undefined);
        state.classList.toggle("shortage", stock !== null && stock !== undefined && stock < item.amount);
        if (stock === undefined) state.textContent = "現在量の入力が正しくありません（0以上、小数第6位まで）。";
        else if (stock === null) state.textContent = "未確認";
        else if (stock < item.amount) state.textContent = `不足 ${format(item.amount - stock)}${item.unit}`;
        else state.textContent = `余裕 ${format(stock - item.amount)}${item.unit}`;
      };
      input.addEventListener("input", updateStock); updateStock();
      row.append(head, label, state, details); materialRoot.append(row);
    });
  }
  cards.forEach(card => {
    const number = card.querySelector('input[name="quantity"]');
    const range = card.querySelector('input[type="range"]');
    number.addEventListener("input", () => {
      if (/^\d+$/.test(number.value) && Number(number.value) <= MAX) range.value = number.value;
      recalculate();
    });
    range.addEventListener("input", () => { number.value = range.value; recalculate(); });
    card.querySelectorAll("[data-step]").forEach(button => button.addEventListener("click", () => {
      const current = Number(number.value);
      number.value = String(Math.max(0, Math.min(MAX, (Number.isInteger(current) ? current : 0) + Number(button.dataset.step))));
      range.value = number.value; recalculate();
    }));
  });
  recalculate();
})();
