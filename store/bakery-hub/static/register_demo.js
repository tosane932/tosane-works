/* 非保存デモ。計算だけを共有し、通信・IndexedDB・sessionStorageを使わない。 */
(function () {
    "use strict";
    const root = document.getElementById("register-root");
    if (!root || !globalThis.RegisterCore) { return; }
    const core = globalThis.RegisterCore;
    const config = JSON.parse(document.getElementById("register-config").textContent);
    if (config.demo !== true) { return; }
    const $ = id => document.getElementById(id);
    const products = config.products;
    const names = new Map(products.map(item => [item.id, item.name]));
    const yen = value => `${new Intl.NumberFormat("ja-JP").format(value)}円`;
    const received = $("received-yen");
    const completeButton = $("complete-checkout");
    const nextButton = $("demo-next");
    const resultMessage = $("demo-result");
    const message = $("register-message");
    let cart = core.createCart(products);
    let pendingRefund = null;
    let completed = false;

    function error(text) { message.textContent = text; message.hidden = false; }
    function clearError() { message.textContent = ""; message.hidden = true; }
    function render() {
        const calculation = core.calculation(cart, received.value);
        for (const tile of $("product-grid").querySelectorAll(".product-tile")) {
            const quantity = cart.quantities.get(Number(tile.dataset.productId)) || 0;
            tile.querySelector(".quantity-input").value = quantity;
            tile.classList.toggle("selected", quantity !== 0);
            tile.querySelector(".quantity-input").disabled = completed;
            for (const button of tile.querySelectorAll("button")) { button.disabled = completed; }
        }
        received.disabled = completed;
        const list = $("cart-lines");
        list.replaceChildren();
        if (!calculation.lines.length) {
            const empty = document.createElement("li");
            empty.className = "empty";
            empty.textContent = "商品はまだありません";
            list.append(empty);
        }
        for (const line of calculation.lines) {
            const row = document.createElement("li");
            const name = document.createElement("span");
            const amount = document.createElement("strong");
            name.textContent = `${line.name} × ${line.quantity}`;
            amount.textContent = yen(line.amount);
            row.append(name, amount);
            list.append(row);
        }
        $("checkout-kind").textContent = !calculation.lines.length ? "商品を選択してください"
            : calculation.kind === "refund" ? "返金の練習 / 完了前に返金額を確認" : "販売の練習";
        $("total-label").textContent = calculation.kind === "refund" ? "返金額" : "合計";
        $("total-yen").textContent = yen(Math.abs(calculation.total));
        $("jump-checkout").textContent = `${calculation.kind === "refund" ? "返金" : "合計"} ${yen(Math.abs(calculation.total))} · 会計を確認`;
        $("cash-area").hidden = calculation.kind === "refund" && calculation.lines.length > 0;
        $("change-yen").textContent = calculation.change === null ? "—" : yen(calculation.change);
        $("cash-hint").textContent = calculation.error && calculation.lines.length
            ? calculation.error : "お預かり金額を入力してください";
        $("cash-hint").classList.toggle("error", Boolean(calculation.error && calculation.lines.length));
        completeButton.disabled = completed || !calculation.valid;
    }
    function change(id, value, approved = false) {
        if (completed) { return; }
        try {
            core.setQuantity(cart, id, value, approved);
            clearError();
            render();
        } catch (failure) {
            if (failure.code === "refund_confirmation") {
                pendingRefund = { id, value };
                render();
                $("refund-dialog-detail").textContent = `${names.get(id)}：${Math.abs(Number(value))}個を返金しますか？`;
                $("refund-dialog").showModal();
            } else { error(failure.message); render(); }
        }
    }
    $("product-grid").addEventListener("click", event => {
        const button = event.target.closest("button[data-action]");
        if (!button || completed) { return; }
        const id = Number(button.closest("[data-product-id]").dataset.productId);
        const quantity = cart.quantities.get(id) || 0;
        change(id, button.dataset.action === "plus" ? Math.min(50, quantity + 1)
            : Math.max(-50, quantity - 1));
    });
    $("product-grid").addEventListener("change", event => {
        if (completed || !event.target.matches(".quantity-input")) { return; }
        change(Number(event.target.closest("[data-product-id]").dataset.productId), event.target.value);
    });
    for (const image of $("product-grid").querySelectorAll("img")) {
        const fallback = () => {
            image.hidden = true;
            image.nextElementSibling.hidden = false;
        };
        image.addEventListener("error", fallback);
        if (image.complete && !image.naturalWidth) { fallback(); }
    }
    received.addEventListener("input", render);
    $("refund-cancel").addEventListener("click", () => {
        pendingRefund = null;
        $("refund-dialog").close();
        render();
    });
    $("refund-confirm").addEventListener("click", () => {
        const requested = pendingRefund;
        pendingRefund = null;
        $("refund-dialog").close();
        if (requested) { change(requested.id, requested.value, true); }
    });
    completeButton.addEventListener("click", () => {
        if (completed || !core.calculation(cart, received.value).valid) { return; }
        completed = true;
        resultMessage.textContent = "練習会計が完了しました。この会計は保存されていません。";
        resultMessage.hidden = false;
        nextButton.hidden = false;
        clearError();
        render();
    });
    nextButton.addEventListener("click", () => {
        cart = core.createCart(products);
        received.value = "";
        completed = false;
        resultMessage.textContent = "";
        resultMessage.hidden = true;
        nextButton.hidden = true;
        clearError();
        render();
        $("catalog-title").scrollIntoView({ block: "start" });
    });
    $("jump-checkout").addEventListener("click", () => $("checkout-title").scrollIntoView({ block: "start" }));
    const back = $("register-back");
    back.addEventListener("click", event => {
        if (completed || (!cart.quantities.size && !received.value)) { return; }
        event.preventDefault();
        $("discard-dialog").showModal();
    });
    $("discard-cancel").addEventListener("click", () => $("discard-dialog").close());
    $("discard-confirm").addEventListener("click", () => location.assign(back.href));
    render();
})();
