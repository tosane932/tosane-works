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
            const request = indexedDB.open("bakery-hub-register-v3", 2);
            request.onupgradeneeded = () => {
                const db = request.result;
                if (!db.objectStoreNames.contains("drafts")) { db.createObjectStore("drafts", { keyPath: "id" }); }
                if (!db.objectStoreNames.contains("queue")) {
                    const queue = db.createObjectStore("queue", { keyPath: "id" });
                    queue.createIndex("scope", "scope");
                }
                if (!db.objectStoreNames.contains("locks")) { db.createObjectStore("locks", { keyPath: "scope" }); }
                if (!db.objectStoreNames.contains("offline")) { db.createObjectStore("offline", { keyPath: "id" }); }
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
    async function recoverPreviousAdminScopes() {
        if (!config.scope.endsWith(":admin")) { return; }
        const prefix = config.scope.slice(0, -"admin".length);
        const records = await operation("queue", "readonly", store => store.getAll());
        for (const record of records) {
            if (record.scope?.startsWith(prefix) && record.scope !== config.scope) {
                // 同一Datasetの旧ログインscopeだけを移す。会計キーとreceiptは不変。
                record.scope = config.scope;
                await operation("queue", "readwrite", store => store.put(record));
            }
        }
    }
    async function updateState() {
        if (!database) { return; }
        const records = await scopeRecords();
        const pending = records.filter(item => item.status === "pending").length;
        const unresolved = records.filter(item => item.status !== "synced").length;
        const synced = records.filter(item => item.status === "synced").length;
        const review = records.filter(item => item.status === "needs_review").length;
        const expired = records.filter(item => item.status === "guest_expired").length;
        const auth = records.filter(item => item.status === "auth_required").length;
        const status = $("sync-state");
        const observation = $("catalog-observation");
        if (observation && !navigator.onLine) {
            observation.textContent = `⚠ オフライン中：最後に取得した商品情報（${config.catalogFetchedAt || "日時不明"}）を使用しています。`;
        } else if (observation) {
            observation.textContent = `商品情報のサーバー取得日時：${config.catalogFetchedAt || "日時不明"}。この画面を開いた後の価格変更は反映されません。`;
        }
        status.classList.toggle("attention", Boolean(pending || review || expired || auth));
        status.textContent = expired ? `⚠ 利用期限が終了しました ${expired}件 / 未同期 ${unresolved}件`
            : review ? `⚠ この会計は確認が必要です。記録は保持されています。${review}件 / 未同期 ${unresolved}件`
            : auth ? `認証の確認が必要です ${auth}件 / 未同期 ${unresolved}件`
            : activeSyncId ? `↻ 会計をサーバーへ同期しています。未同期 ${unresolved}件`
            : pending && (!navigator.onLine || records.some(item => item.status === "pending" && item.attempts > 0))
                ? `◷ 端末に記録済み・サーバー反映未確認 ${pending}件（通信後に再送）`
            : pending ? `◷ 端末に記録済み・サーバー反映未確認 ${pending}件`
            : `✓ サーバー保存済み ${synced}件 / 未同期 0件`;
        const check = $("check-register-status");
        if (check) { check.disabled = !database || syncing || !records.length; }
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
                return `${reference}：営業日が変わったため確認が必要です。自動で再登録しません。端末の記録を保持しています。`;
            }
            if (item.reason === "price_changed" && Array.isArray(item.differences)) {
                const detail = item.differences.map(diff => {
                    const snapshot = local.snapshots.find(line => line.id === diff.product_id);
                    const name = snapshot ? snapshot.name : diff.name;
                    const quantity = snapshot ? snapshot.quantity : 0;
                    return `${name} ${yen(diff.displayed_yen)} → 現在 ${yen(diff.current_yen)}（単価差 ${yen(diff.current_yen - diff.displayed_yen)}、${quantity}個の会計差 ${yen((diff.current_yen - diff.displayed_yen) * quantity)}）`;
                }).join("、");
                return `${reference}：価格変更を確認してください。${detail}。自動で再登録しません。`;
            }
            if (item.reason === "key_conflict") {
                return `${reference}：同じ会計キーの保存済み内容と端末の内容が異なります。自動で再登録しないでください。`;
            }
            return `${reference}：自動同期できません。端末の記録を保持しています。`;
        }).join("\n");
        for (const item of records) {
            const receipt = item.receipt;
            const details = document.createElement("details");
            const summary = document.createElement("summary");
            summary.textContent = `${receipt.client_business_date} ${receipt.kind === "refund" ? "返金" : "販売"}・${item.status}・${receipt.submission_key}`;
            const data = document.createElement("pre");
            data.textContent = [
                `送信キー: ${receipt.submission_key}`,
                `状態: ${item.status} / 理由: ${item.reason || "なし"}`,
                `サーバー照合: ${item.serverStatus || "未確認"}`,
                `申告営業日: ${receipt.client_business_date}`,
                `端末申告時刻（証明ではありません）: ${receipt.completed_at_local}`,
                `区分: ${receipt.kind} / 合計: ${yen(receipt.total_yen)}`,
                `お預かり: ${receipt.received_yen === null ? "なし" : yen(receipt.received_yen)}`,
                `返金確認: ${receipt.refund_confirmed ? "あり" : "なし"}`,
                ...receipt.snapshots.map(line =>
                    `商品ID ${line.id} ${line.name}・${line.quantity}個 × 表示単価 ${yen(line.unit_price_yen)} = ${yen(line.line_total_yen)}`),
            ].join("\n");
            details.append(summary, data);
            if (config.recoveryUrl && item.status !== "synced") {
                const button = document.createElement("button");
                button.type = "button";
                button.textContent = item.backedUp ? "保全済み案件を再照合" : "この元記録をサーバーへ保全";
                button.addEventListener("click", () => preserveForReview(item));
                details.append(button);
                if (item.backedUp) {
                    const notice = document.createElement("p");
                    notice.textContent = "サーバー保全済み（売上確定ではありません）。端末の元記録も保持中。";
                    details.append(notice);
                }
            }
            recovery.append(details);
        }
        recovery.hidden = !records.length;
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
        checkoutButton.disabled = !config.saveEnabled || !database || busy || !result.valid
            || ["denied", "checking"].includes(globalThis.RegisterOfflineAuth);
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
            if (["denied", "checking"].includes(globalThis.RegisterOfflineAuth)) {
                throw Error("認証または端末許可が失効しました。Adminがオンラインで再準備してください。");
            }
            if (config.offlineGrant && (Date.now() > Date.parse(config.offlineExpiresAt)
                || !config.approvedDays.includes(currentDay()))) {
                throw Error("承認された営業日または端末利用期限を過ぎました。オンラインで再準備してください。");
            }
            if (config.offlineMode && !config.offlineGrant && !navigator.onLine) {
                throw Error("オフライン利用には端末の事前準備が必要です。");
            }
            const receipt = core.receipt(cart, receivedInput.value, key);
            if (config.catalogProof) { receipt.catalog_proof = config.catalogProof; }
            clearTimeout(draftTimer);
            await draftWrite.catch(() => {}); // 下書きの失敗は原子的なqueue保存で再試行する。
            await checkoutToLocal({ id: `${config.scope}:${key}`, scope: config.scope,
                                    status: "pending", receipt, grantToken: config.offlineGrant || null,
                                    attempts: 0, nextRetryAt: 0 });
            cart = core.createCart(products);
            receivedInput.value = "";
            key = uuid();
            clearError();
            if ($("checkout-result")) { $("checkout-result").textContent = "✓ 会計完了・端末に記録済み。サーバーへの反映待ちです。"; }
            await updateState();
            kickSync();
        } catch (error) {
            showError(`⚠ 会計を記録できませんでした。入力内容を保持しています。${error.message || "保存領域を確認してください。"}`);
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
    function matchesSaved(saved, receipt) {
        const samePairs = (left, right) => {
            if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) { return false; }
            const actual = [...left].sort((a, b) => a[0] - b[0]);
            const expected = [...right].sort((a, b) => a[0] - b[0]);
            return actual.every(([id, value], index) =>
                id === expected[index][0] && value === expected[index][1]);
        };
        return saved.status === "saved" && saved.submission_key === receipt.submission_key
            && saved.transaction_id && saved.kind === receipt.kind
            && saved.business_date === receipt.client_business_date
            && saved.total_yen === receipt.total_yen
            && saved.received_yen === receipt.received_yen
            && samePairs(saved.items, receipt.items)
            && samePairs(saved.displayed_prices, receipt.displayed_prices);
    }
    async function preserveForReview(item) {
        if (!config.recoveryUrl || syncing || !navigator.onLine) {
            $("status-check-result").textContent = "通信できません。端末の元記録は保持しています。";
            return;
        }
        syncing = true;
        try {
            const response = await fetch(config.recoveryUrl, {
                method: "POST", credentials: "same-origin",
                headers: { "Content-Type": "application/json",
                           "X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content },
                body: JSON.stringify({ receipt: item.receipt, device_status: item.status }),
            });
            const result = response.headers.get("Content-Type")?.includes("application/json")
                ? await response.json() : {};
            if (!response.ok || !result.case_id) { throw new Error(result.error || "保全できませんでした。"); }
            // サーバー保全は売上確定でも端末キューの消去でもない。
            item.backedUp = result.case_id;
            item.status = "needs_review"; // 照合GETが完全一致するまで同期済みにはしない。
            item.reason = "recovery_review";
            await operation("queue", "readwrite", store => store.put(item));
            $("status-check-result").textContent = `元記録を保全しました。${result.status === "saved" ? "保存済み会計と内容を照合しました。" : "売上には計上していません。管理者の調査が必要です。"}`;
        } catch (error) {
            $("status-check-result").textContent = `${error.message} 端末の元記録は保持しています。`;
        } finally { syncing = false; await updateState().catch(() => {}); }
    }
    async function checkSavedRecords() {
        if (syncing || !database || !navigator.onLine) {
            $("status-check-result").textContent = "通信できません。端末の記録は保持しています。";
            return;
        }
        syncing = true;
        const run = async () => {
            const records = (await scopeRecords()).filter(item => item.status !== "synced");
            let confirmed = 0;
            for (const record of records) {
                try {
                    const response = await fetch(`${config.statusBase}/${encodeURIComponent(record.receipt.submission_key)}/status`,
                        { method: "GET", credentials: "same-origin" });
                    if (!response.headers.get("Content-Type")?.includes("application/json")) {
                        throw new Error("認証を確認してください。");
                    }
                    const saved = await response.json();
                    if (!response.ok) { throw new Error(saved.error || "照会できませんでした。"); }
                    if (saved.status === "saved") {
                        if (matchesSaved(saved, record.receipt)) {
                            record.status = "synced";
                            record.transactionId = saved.transaction_id;
                            record.reason = null;
                            confirmed++;
                        } else {
                            record.status = "needs_review";
                            record.reason = "key_conflict";
                        }
                        await operation("queue", "readwrite", store => store.put(record));
                    }
                    record.serverStatus = saved.status === "saved" ? "保存済み（内容を照合）" : "未発見（未保存の証明ではありません）";
                    await operation("queue", "readwrite", store => store.put(record));
                    // not_found は未保存の証明ではない。状態も送信キーも変えない。
                } catch (_) {
                    $("status-check-result").textContent = "照会を完了できませんでした。端末の記録を保持しています。";
                    await updateState();
                    return;
                }
            }
            $("status-check-result").textContent = confirmed
                ? `${confirmed}件はサーバーに保存済みです（同じ会計キー・明細・金額を照合）。ほかの会計は端末の記録を保持しています。`
                : "サーバーへの反映を確認できませんでした。端末の記録を保持しています。";
            await updateState();
        };
        try {
            if (navigator.locks?.request) {
                await navigator.locks.request(`bakery-register-sync:${config.scope}`,
                    { ifAvailable: true }, async lock => {
                        if (lock) { await run(); }
                        else { $("status-check-result").textContent = "別のタブが同期中です。しばらくしてから確認してください。"; }
                    });
            } else if (await lease()) {
                try { await run(); } finally { await releaseLease(); }
            } else { $("status-check-result").textContent = "別のタブが同期中です。"; }
        } catch (_) { $("status-check-result").textContent = "照会できませんでした。端末の記録を保持しています。"; }
        finally { syncing = false; await updateState().catch(() => {}); }
    }
    $("check-register-status").addEventListener("click", checkSavedRecords);
    async function send(record) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 12000);
        try {
            if (record.grantToken) {
                const check = await fetch(`${config.statusBase}/${encodeURIComponent(record.receipt.submission_key)}/status`,
                    { method: "GET", credentials: "same-origin", signal: controller.signal });
                if (!check.ok || !check.headers.get("Content-Type")?.includes("application/json")) {
                    return { status: [401, 403].includes(check.status) || check.redirected ? "auth_required" : "pending",
                             reason: "status_unavailable" };
                }
                const existing = await check.json();
                if (existing.status === "saved") {
                    if (matchesSaved(existing, record.receipt)) {
                        record.transactionId = existing.transaction_id;
                        return { status: "synced" };
                    }
                    return { status: "needs_review", reason: "key_conflict" };
                }
                if (existing.status !== "not_found") {
                    return { status: "needs_review", reason: "status_unknown" };
                }
                // not_found is not proof of non-delivery; the same key is enforced under Dataset lock.
            }
            const response = await fetch(record.grantToken ? config.offlineSyncUrl : config.postUrl, {
                method: "POST", credentials: "same-origin", signal: controller.signal,
                headers: { "Content-Type": "application/json",
                           "X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content },
                body: JSON.stringify(record.grantToken ?
                    { receipt: record.receipt, grant_token: record.grantToken } : payload(record.receipt)),
            });
            const type = response.headers.get("Content-Type") || "";
            if (!type.includes("application/json")) {
                return { status: "auth_required", reason: "non_json_response" };
            }
            const result = await response.json();
            if (response.ok && result.submission_key === record.receipt.submission_key
                && result.total_yen === record.receipt.total_yen && result.transaction_id
                && result.business_date === record.receipt.client_business_date) {
                try {
                    const check = await fetch(`${config.statusBase}/${encodeURIComponent(record.receipt.submission_key)}/status`,
                        { method: "GET", credentials: "same-origin" });
                    const saved = check.headers.get("Content-Type")?.includes("application/json")
                        ? await check.json() : null;
                    if (check.ok && matchesSaved(saved, record.receipt)) {
                        record.transactionId = result.transaction_id;
                        return { status: "synced" };
                    }
                    if (check.ok && saved?.status === "saved") {
                        return { status: "needs_review", reason: "key_conflict" };
                    }
                } catch (_) { /* 応答不明のまま元記録を保持する */ }
                return { status: "pending", reason: "communication" };
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
            if (response.status === 409 && result.code === "offline_review") {
                return { status: "needs_review", reason: "offline_review" };
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
            if (!record.grantToken && record.receipt.client_business_date !== currentDay()) {
                record.status = "needs_review";
                record.reason = "business_day_changed";
            } else if (record.backedUp) {
                record.status = "needs_review";
                record.reason = "recovery_review";
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
        if (!config.saveEnabled || !database || syncing || !navigator.onLine
            || (config.offlineGrant && globalThis.RegisterOfflineAuth !== "ready")) { return; }
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
    window.addEventListener("register-auth-ready", kickSync);
    window.addEventListener("register-auth-change", render);
    window.addEventListener("offline", () => updateState().catch(() => {}));
    window.addEventListener("focus", kickSync);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { kickSync(); } });

    async function start() {
        try {
            key = uuid();
            database = await openDatabase();
            await recoverPreviousAdminScopes();
            draftId = config.offlineGrant
                ? `offline-draft:${config.scope}:${config.deviceId}` : tabId();
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
