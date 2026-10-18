/* Cash entry only. Checkout calculations, draft persistence and receipts stay in their owners. */
(function (root) {
    "use strict";
    const MAX_RECEIVED = 1000000000;
    function edit(current, key) {
        const value = /^(0|[1-9]\d*)$/.test(current) && Number(current) <= MAX_RECEIVED ? String(current) : "";
        if (key === "clear") { return ""; }
        if (key === "backspace") { return value.slice(0, -1); }
        if (!/^(\d|00)$/.test(key)) { return value; }
        const next = (value + key).replace(/^0+(?=\d)/, "");
        return Number(next) <= MAX_RECEIVED ? next : value;
    }
    function mount(input) {
        const panel = document.getElementById("cash-keypad");
        const display = document.getElementById("received-display");
        const notice = document.getElementById("cash-keypad-notice");
        let locked = true;
        function refresh(disabled = false) {
            locked = disabled;
            if (!input.value) { notice.textContent = ""; }
            display.textContent = input.value === "" ? "—" :
                /^(0|[1-9]\d*)$/.test(input.value) ? new Intl.NumberFormat("ja-JP").format(Number(input.value)) : "入力を確認";
            for (const button of panel.querySelectorAll("button")) { button.disabled = disabled; }
        }
        function enter(key) {
            if (locked || document.getElementById("cash-area").hidden) { return; }
            const next = edit(input.value, key);
            notice.textContent = /^(\d|00)$/.test(key) && next === input.value && Number(input.value + key) > MAX_RECEIVED
                ? "お預かりは1,000,000,000円までです。1桁削除またはCで修正してください。" : "";
            if (next !== input.value) {
                input.value = next;
                input.dispatchEvent(new Event("input", { bubbles: true }));
            }
            refresh(locked);
        }
        panel.addEventListener("click", event => {
            const button = event.target.closest("button[data-cash-key]");
            if (button) { enter(button.dataset.cashKey); }
        });
        panel.addEventListener("keydown", event => {
            if (event.ctrlKey || event.metaKey || event.altKey) { return; }
            const key = /^\d$/.test(event.key) ? event.key :
                event.key === "Backspace" ? "backspace" :
                ["Delete", "Escape", "c", "C"].includes(event.key) ? "clear" : null;
            if (key !== null) { event.preventDefault(); enter(key); }
        });
        return { refresh };
    }
    const api = { edit, mount };
    if (typeof module !== "undefined" && module.exports) { module.exports = api; }
    else { root.RegisterCashPad = api; }
})(globalThis);
