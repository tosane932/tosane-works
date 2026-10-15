/* 会計中の計算は通信・DOM・DBに依存させない。サーバーでの再計算が正本。 */
(function (root) {
    "use strict";
    const MAX_RECEIVED = 1000000000;

    function integer(value, min, max) {
        const text = String(value);
        if (typeof value === "boolean" || !/^-?(0|[1-9]\d*)$/.test(text) || text.length > 11) {
            throw new Error("金額・個数は整数で入力してください。");
        }
        const number = Number(text);
        if (!Number.isSafeInteger(number) || number < min || number > max) {
            throw new Error("入力できる範囲を超えています。");
        }
        return number;
    }

    function createCart(products, restored) {
        const available = new Map(products.map(product => [product.id, product]));
        const quantities = new Map();
        if (restored && Array.isArray(restored)) {
            for (const [id, value] of restored) {
                if (!available.has(id) || quantities.has(id)) { continue; }
                try {
                    const qty = integer(value, -50, 50);
                    if (qty && ![...quantities.values()].some(other => Math.sign(other) !== Math.sign(qty))) {
                        quantities.set(id, qty);
                    }
                } catch (_) { /* 削除・無効化された商品や壊れた下書きは売上にしない */ }
            }
        }
        return { available, quantities };
    }

    function setQuantity(cart, id, value, refundConfirmed = false) {
        if (!cart.available.has(id)) { throw new Error("商品が見つかりません。"); }
        const quantity = integer(value, -50, 50);
        const prior = cart.quantities.get(id) || 0;
        for (const [otherId, otherQty] of cart.quantities) {
            if (otherId !== id && otherQty && quantity && Math.sign(otherQty) !== Math.sign(quantity)) {
                throw new Error("販売と返金は別会計にしてください。");
            }
        }
        if (quantity < 0 && prior >= 0 && !refundConfirmed) {
            const error = new Error("返金の確認が必要です。");
            error.code = "refund_confirmation";
            throw error;
        }
        if (quantity) { cart.quantities.set(id, quantity); }
        else { cart.quantities.delete(id); }
        return quantity;
    }

    function lines(cart) {
        return Array.from(cart.quantities, ([id, quantity]) => ({
            id, name: cart.available.get(id).name,
            price: cart.available.get(id).price, quantity,
            amount: cart.available.get(id).price * quantity,
        })).sort((a, b) => a.id - b.id);
    }

    function calculation(cart, receivedText) {
        const selected = lines(cart);
        if (selected.some(line => Math.sign(line.quantity) !== Math.sign(selected[0].quantity))) {
            return { lines: selected, kind: "sale", total: 0, received: null, change: null,
                     valid: false, error: "販売と返金は別会計にしてください。" };
        }
        const total = selected.reduce((sum, line) => sum + line.amount, 0);
        const kind = selected.length && selected[0].quantity < 0 ? "refund" : "sale";
        if (!selected.length || !total) {
            return { lines: selected, kind, total, received: null, change: null, valid: false,
                     error: selected.length ? "0円会計は現在保存できません。" : "商品を選んでください。" };
        }
        if (kind === "refund") {
            return { lines: selected, kind, total, received: null, change: null, valid: true, error: null };
        }
        try {
            const received = integer(receivedText, 0, MAX_RECEIVED);
            if (received < total) { throw new Error("お預かり金額が不足しています。"); }
            return { lines: selected, kind, total, received, change: received - total,
                     valid: true, error: null };
        } catch (error) {
            return { lines: selected, kind, total, received: null, change: null,
                     valid: false, error: error.message };
        }
    }

    function tokyoDay(date) {
        const parts = new Intl.DateTimeFormat("en-US", {
            timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit",
        }).formatToParts(date);
        const part = type => parts.find(value => value.type === type).value;
        return `${part("year")}-${part("month")}-${part("day")}`;
    }

    function receipt(cart, receivedText, submissionKey, date = new Date()) {
        const result = calculation(cart, receivedText);
        if (!result.valid) { throw new Error(result.error); }
        return {
            submission_key: submissionKey,
            kind: result.kind,
            items: result.lines.map(line => [line.id, line.quantity]),
            displayed_prices: result.lines.map(line => [line.id, line.price]),
            snapshots: result.lines.map(line => ({ id: line.id, name: line.name,
                unit_price_yen: line.price, quantity: line.quantity, line_total_yen: line.amount })),
            total_yen: result.total,
            received_yen: result.received,
            change_yen: result.change,
            refund_confirmed: result.kind === "refund",
            client_business_date: tokyoDay(date),
            completed_at_local: date.toISOString(),
        };
    }

    const api = { integer, createCart, setQuantity, lines, calculation, tokyoDay, receipt };
    if (typeof module !== "undefined" && module.exports) { module.exports = api; }
    else { root.RegisterCore = api; }
})(globalThis);
