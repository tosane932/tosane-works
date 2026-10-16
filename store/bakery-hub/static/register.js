/* 開いているレジだけで動作。商品操作ではHTTP通信せず、確定済み会計をIndexedDBへ保管する。 */
(function () {
    "use strict";
    const root = document.getElementById("register-root");
    if (!root || !globalThis.RegisterCore) { return; }
    const core = globalThis.RegisterCore;
    const config = JSON.parse(document.getElementById("register-config").textContent);
    const products = config.products;
    const productNames = new Map(products.map(item => [item.id, item.name]));
    const $ = id => document.getElementById(id);
    const message = $("register-message");
    const checkoutButton = $("complete-checkout");
    const receivedInput = $("received-yen");
    const formatter = new Intl.NumberFormat("ja-JP");
    const yen = number => `${formatter.format(number)}円`;
    let cart = core.createCart(products);
    let key;
    let database;
    let busy = false;
    let syncing = false;
    let activeSyncId;
    let retryTimer;
    let draftTimer;
    let draftWrite = Promise.resolve();
    let pendingRefund;

    function showError(text) {
        message.textContent = text;
        message.hidden = false;
    }
    function clearError() { message.hidden = true; message.textContent = ""; }
    function uuid() {
        if (!globalThis.crypto || !crypto.randomUUID) { throw new Error("安全な会計キーを発行できません。"); }
        return crypto.randomUUID();
    }
    function currentDay() { return core.tokyoDay(new Date()); }

    function openDatabase() {
        return new Promise((resolve, reject) => {
            if (!globalThis.indexedDB) { reject(new Error("IndexedDBを使用できません。")); return; }
            const request = indexedDB.open("bakery-hub-register-v3", 1);
            request.onupgradeneeded = () => {
                const db = request.result;
                if (!db.objectStoreNames.contains("drafts")) { db.createObjectStore("drafts", { keyPath: "id" }); }
                if (!db.objectStoreNames.contains("queue")) {
                    const queue = db.createObjectStore("queue", { keyPath: "id" });
                    queue.createIndex("scope", "scope");
                }
                if (!db.objectStoreNames.contains("locks")) { db.createObjectStore("locks", { keyPath: "scope" }); }
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
            request.onblocked = () => reject(new Error("別のタブが保存領域を使用しています。"));
        });
    }
    function operation(storeName, mode, action) {
        return new Promise((resolve, reject) => {
            let result;
            const tx = database.transaction(storeName, mode);
            const store = tx.objectStore(storeName);
            const request = action(store);
            if (request) { request.onsuccess = () => { result = request.result; }; }
            tx.oncomplete = () => resolve(result);
            tx.onerror = () => reject(tx.error || new Error("ローカル保存に失敗しました。"));
            tx.onabort = () => reject(tx.error || new Error("ローカル保存が中断されました。"));
        });
    }
    function tabId() {
        const name = `bakery-register-tab:${config.scope}`;
        let value = sessionStorage.getItem(name);
        if (!value) { value = uuid(); sessionStorage.setItem(name, value); }
        return `${config.scope}:${value}`;
    }
    let draftId;
    function persistDraft() {
        const snapshot = { id: draftId, key, quantities: [...cart.quantities],
                           received: receivedInput.value };
        draftWrite = draftWrite.catch(() => {}).then(
            () => operation("drafts", "readwrite", store => store.put(snapshot)));
        draftWrite.catch(() => showError("入力中の会計を端末に保持できません。保存領域を確認してください。"));
    }
    function scheduleDraft() {
        clearTimeout(draftTimer);
        draftTimer = setTimeout(persistDraft, 220);
    }
    function checkoutToLocal(record) {
        return new Promise((resolve, reject) => {
            const tx = database.transaction(["drafts", "queue"], "readwrite");
            tx.objectStore("queue").add(record);
            tx.objectStore("drafts").delete(draftId);
            tx.oncomplete = resolve;
            tx.onerror = () => reject(tx.error || new Error("端末内への保存に失敗しました。"));
            tx.onabort = () => reject(tx.error || new Error("端末内への保存が中断されました。"));
        });
    }
    function scopeRecords() {
        return operation("queue", "readonly", store => store.index("scope").getAll(config.scope));
    }
    async function updateState() {
        if (!database) { return; }
        const records = await scopeRecords();
        const pending = records.filter(item => item.status === "pending").length;
        const review = records.filter(item => item.status === "needs_review").length;
        const expired = records.filter(item => item.status === "guest_expired").length;
        const auth = records.filter(item => item.status === "auth_required").length;
        const status = $("sync-state");
        status.classList.toggle("attention", Boolean(pending || review || expired || auth));
        status.textContent = expired ? `利用期限が終了しました ${expired}件 / 未同期 ${pending}件`
            : review ? `確認が必要な会計があります ${review}件 / 未同期 ${pending}件`
            : auth ? `認証の確認が必要です ${auth}件 / 未同期 ${pending}件`
            : activeSyncId ? `同期しています 未同期 ${pending}件`
            : pending && (!navigator.onLine || records.some(item => item.status === "pending" && item.attempts > 0))
                ? `通信できません。再送します 未同期 ${pending}件`
                : pending ? `送信待ち 未同期 ${pending}件` : "同期済み";
        const recovery = $("recovery-list");
        const flagged = records.filter(item => ["needs_review", "guest_expired", "auth_required"].includes(item.status));
        recovery.hidden = flagged.length === 0;
        recovery.textContent = flagged.map(item => {
            const local = item.receipt;
            const at = new Date(local.completed_at_local);
            const time = Number.isNaN(at.getTime()) ? "時刻不明" : new Intl.DateTimeFormat("ja-JP", {
                timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit",
            }).format(at);
            const reference = `会計 ${local.client_business_date} ${time} / ${local.kind === "refund" ? "返金" : "販売"} ${yen(Math.abs(local.total_yen))}`;
            if (item.status === "guest_expired") {
                return `${reference}：利用期限が終了しました。端末の記録を保持しています。別のGuestへ移せません。`;
            }
            if (item.status === "auth_required") {
                return `${reference}：認証を確認してください。端末の記録は未同期です。`;
            }
            if (item.reason === "business_day_changed") {
                return `${reference}：営業日が変わったため自動同期できません。端末の記録を保持しています。`;
            }
            if (item.reason === "price_changed" && Array.isArray(item.differences)) {
                const detail = item.differences.map(diff => {
                    const snapshot = local.snapshots.find(line => line.id === diff.product_id);
                    const name = snapshot ? snapshot.name : diff.name;
                    const quantity = snapshot ? snapshot.quantity : 0;
                    return `${name} ${yen(diff.displayed_yen)} → 現在 ${yen(diff.current_yen)}（単価差 ${yen(diff.current_yen - diff.displayed_yen)}、${quantity}個の会計差 ${yen((diff.current_yen - diff.displayed_yen) * quantity)}）`;
                }).join("、");
                return `${reference}：価格変更を確認してください。${detail}。自動確定していません。`;
            }
            return `${reference}：自動同期できません。端末の記録を保持しています。`;
        }).join("\n");
    }

    function render() {
        const result = core.calculation(cart, receivedInput.value);
        for (const tile of $("product-grid").children) {
            const quantity = cart.quantities.get(Number(tile.dataset.productId)) || 0;
            tile.querySelector(".quantity-input").value = quantity;
            tile.classList.toggle("selected", quantity !== 0);
        }
        const list = $("cart-lines");
        list.replaceChildren();
        if (!result.lines.length) {
            const empty = document.createElement("li");
            empty.className = "empty";
            empty.textContent = "商品はまだありません";
            list.append(empty);
        }
        for (const line of result.lines) {
            const row = document.createElement("li");
            const name = document.createElement("span");
            name.textContent = `${line.name} × ${line.quantity}`;
            const amount = document.createElement("strong");
            amount.textContent = yen(line.amount);
            row.append(name, amount);
            list.append(row);
        }
        $("checkout-kind").textContent = !result.lines.length ? "商品を選択してください"
            : result.kind === "refund" ? "返金会計 / 会計完了前に返金額を確認" : "販売会計";
        $("total-label").textContent = result.kind === "refund" ? "返金額" : "合計";
        $("total-yen").textContent = yen(result.kind === "refund" ? -result.total : result.total);
        $("jump-checkout").textContent = `${result.kind === "refund" ? "返金" : "合計"} ${yen(Math.abs(result.total))} · 会計を確認`;
        $("cash-area").hidden = result.kind === "refund" && result.lines.length > 0;
        $("change-yen").textContent = result.change === null ? "—" : yen(result.change);
        $("cash-hint").textContent = result.error && result.lines.length ? result.error : "お預かり金額を入力してください";
        $("cash-hint").classList.toggle("error", Boolean(result.error && result.lines.length));
        checkoutButton.disabled = !config.saveEnabled || !database || busy || !result.valid;
    }
    function change(id, value, approved = false) {
        try {
            core.setQuantity(cart, id, value, approved);
            clearError(); render(); scheduleDraft();
        } catch (error) {
            if (error.code === "refund_confirmation") {
                pendingRefund = { id, value };
                render(); // 確認前の入力値を確定数量として表示しない。
                $("refund-dialog-detail").textContent = `${productNames.get(id)}：${Math.abs(Number(value))}個を返金しますか？`;
                $("refund-dialog").showModal();
            } else { showError(error.message); render(); }
        }
    }
    $("product-grid").addEventListener("click", event => {
        const button = event.target.closest("button[data-action]");
        if (!button) { return; }
        const id = Number(button.closest("[data-product-id]").dataset.productId);
        const quantity = cart.quantities.get(id) || 0;
        change(id, button.dataset.action === "plus" ? Math.min(50, quantity + 1)
            : Math.max(-50, quantity - 1));
    });
    $("product-grid").addEventListener("change", event => {
        if (!event.target.matches(".quantity-input")) { return; }
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
    receivedInput.addEventListener("input", () => { render(); scheduleDraft(); });
    $("refund-cancel").addEventListener("click", () => { pendingRefund = null; $("refund-dialog").close(); render(); });
    $("refund-confirm").addEventListener("click", () => {
        const requested = pendingRefund;
        pendingRefund = null;
        $("refund-dialog").close();
        if (requested) { change(requested.id, requested.value, true); }
    });

    async function complete() {
        if (busy || !database || !config.saveEnabled) { return; }
        busy = true; render();
        try {
            const receipt = core.receipt(cart, receivedInput.value, key);
            clearTimeout(draftTimer);
            await draftWrite.catch(() => {}); // 下書きの失敗は原子的なqueue保存で再試行する。
            await checkoutToLocal({ id: `${config.scope}:${key}`, scope: config.scope,
                                    status: "pending", receipt, attempts: 0, nextRetryAt: 0 });
            cart = core.createCart(products);
            receivedInput.value = "";
            key = uuid();
            clearError();
            await updateState();
            kickSync();
        } catch (error) {
            showError(`会計記録を保存できませんでした。${error.message || "保存領域を確認してください。"}`);
        } finally { busy = false; render(); }
    }
    checkoutButton.addEventListener("click", complete);
    $("jump-checkout").addEventListener("click", () => $("checkout-title").scrollIntoView({ block: "start" }));

    const back = $("register-back");
    back.addEventListener("click", event => {
        if (!cart.quantities.size && !receivedInput.value) { return; }
        event.preventDefault();
        $("discard-dialog").showModal();
    });
    $("discard-cancel").addEventListener("click", () => $("discard-dialog").close());
    $("discard-confirm").addEventListener("click", async () => {
        try {
            clearTimeout(draftTimer);
            await draftWrite;
            await operation("drafts", "readwrite", store => store.delete(draftId));
            location.assign(back.href); // queueの未同期会計には触れない。
        } catch (_) { showError("下書きを破棄できませんでした。端末の保存領域を確認してください。"); }
    });

    let lockOwner;
    async function lease() {
        lockOwner = uuid();
        const now = Date.now();
        return new Promise((resolve, reject) => {
            let granted = false;
            const tx = database.transaction("locks", "readwrite");
            const store = tx.objectStore("locks");
            const existing = store.get(config.scope);
            existing.onsuccess = () => {
                if (!existing.result || existing.result.until < now) {
                    store.put({ scope: config.scope, owner: lockOwner, until: now + 30000 });
                    granted = true;
                }
            };
            tx.oncomplete = () => resolve(granted);
            tx.onerror = () => reject(tx.error);
            tx.onabort = () => reject(tx.error);
        });
    }
    async function releaseLease() {
        const owner = lockOwner;
        await new Promise((resolve, reject) => {
            const tx = database.transaction("locks", "readwrite");
            const store = tx.objectStore("locks");
            const existing = store.get(config.scope);
            existing.onsuccess = () => {
                if (existing.result && existing.result.owner === owner) { store.delete(config.scope); }
            };
            tx.oncomplete = resolve;
            tx.onerror = () => reject(tx.error);
            tx.onabort = () => reject(tx.error);
        });
    }
    function retrySoon(delay) {
        clearTimeout(retryTimer);
        retryTimer = setTimeout(kickSync, Math.max(1000, delay));
    }
    function payload(receipt) {
        return { submission_key: receipt.submission_key, kind: receipt.kind,
                 items: receipt.items, displayed_prices: receipt.displayed_prices,
                 received_yen: receipt.received_yen, refund_confirmed: receipt.refund_confirmed,
                 client_business_date: receipt.client_business_date };
    }
    async function send(record) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 12000);
        try {
            const response = await fetch(config.postUrl, {
                method: "POST", credentials: "same-origin", signal: controller.signal,
                headers: { "Content-Type": "application/json",
                           "X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content },
                body: JSON.stringify(payload(record.receipt)),
            });
            const type = response.headers.get("Content-Type") || "";
            if (!type.includes("application/json")) {
                return { status: "auth_required", reason: "non_json_response" };
            }
            const result = await response.json();
            if (response.ok && result.submission_key === record.receipt.submission_key
                && result.total_yen === record.receipt.total_yen && result.transaction_id
                && result.business_date === record.receipt.client_business_date) {
                record.transactionId = result.transaction_id;
                return { status: "synced" };
            }
            if (response.status === 403 && result.code === "guest_expired") {
                return { status: "guest_expired", reason: "guest_expired" };
            }
            if (response.status === 401 || result.code === "auth_required") {
                return { status: "auth_required", reason: "auth_required" };
            }
            if (response.status === 409 && result.code === "price_changed") {
                return { status: "needs_review", reason: "price_changed",
                         differences: Array.isArray(result.differences) ? result.differences : [] };
            }
            if (response.status === 409 && result.code === "business_day_changed") {
                return { status: "needs_review", reason: "business_day_changed" };
            }
            return (response.status >= 400 && response.status < 500 && response.status !== 429)
                || response.ok ? { status: "needs_review", reason: result.code || "unexpected_response" }
                    : { status: "pending", reason: "communication" };
        } catch (_) { return { status: "pending", reason: "communication" }; }
        finally { clearTimeout(timeout); }
    }
    async function flushQueue() {
        const records = (await scopeRecords()).filter(item => item.status === "pending")
            .sort((a, b) => a.receipt.completed_at_local.localeCompare(b.receipt.completed_at_local));
        for (const record of records) {
            if (record.nextRetryAt > Date.now()) { continue; }
            if (record.receipt.client_business_date !== currentDay()) {
                record.status = "needs_review";
                record.reason = "business_day_changed";
            } else {
                activeSyncId = record.id;
                await updateState();
                const outcome = await send(record);
                activeSyncId = null;
                record.status = outcome.status;
                record.reason = outcome.reason || null;
                record.differences = outcome.differences || null;
                if (record.status === "pending") {
                    record.attempts += 1;
                    record.nextRetryAt = Date.now() + Math.min(60000, 1500 * (2 ** Math.min(record.attempts, 6)));
                }
            }
            await operation("queue", "readwrite", store => store.put(record));
            await updateState();
        }
        const next = (await scopeRecords()).filter(item => item.status === "pending");
        if (next.length) { retrySoon(Math.min(...next.map(item => item.nextRetryAt - Date.now()))); }
        else { clearTimeout(retryTimer); }
    }
    async function kickSync() {
        if (!config.saveEnabled || !database || syncing || !navigator.onLine) { return; }
        syncing = true;
        try {
            if (navigator.locks && navigator.locks.request) {
                await navigator.locks.request(`bakery-register-sync:${config.scope}`,
                    { ifAvailable: true }, async lock => {
                        if (lock) { await flushQueue(); }
                        else { retrySoon(3000); }
                    });
            } else if (await lease()) {
                try { await flushQueue(); } finally { await releaseLease(); }
            } else { retrySoon(3000); }
        } catch (_) { retrySoon(10000); }
        finally {
            syncing = false;
            activeSyncId = null;
            try { await updateState(); } catch (_) { /* 次の起動時に再確認する */ }
        }
    }
    window.addEventListener("online", kickSync);
    window.addEventListener("offline", () => updateState().catch(() => {}));
    window.addEventListener("focus", kickSync);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { kickSync(); } });

    async function start() {
        try {
            key = uuid();
            database = await openDatabase();
            draftId = tabId();
            const draft = await operation("drafts", "readonly", store => store.get(draftId));
            if (draft) {
                cart = core.createCart(products, draft.quantities);
                receivedInput.value = draft.received || "";
                key = draft.key || key;
            }
            // 認証済みの同じスコープでページを再取得した場合だけ、
            // 新しいCSRFを使って同一キーの再送を試みる。Guest期限切れは解除しない。
            const records = await scopeRecords();
            for (const record of records.filter(item => item.status === "auth_required")) {
                record.status = "pending";
                record.nextRetryAt = 0;
                await operation("queue", "readwrite", store => store.put(record));
            }
            await updateState();
            render();
            kickSync();
        } catch (_) {
            database = null;
            showError("会計記録を保存できませんでした。端末の保存領域を利用できないため会計完了を停止しています。");
            $("sync-state").textContent = "保存領域を利用できません";
            render();
        }
    }
    render();
    start();
})();
