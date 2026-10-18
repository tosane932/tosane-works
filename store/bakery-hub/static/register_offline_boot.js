/* The cached shell contains no account data. Unlock the encrypted catalog in this tab. */
(function () {
    "use strict";
    const form = document.getElementById("offline-unlock");
    if (!form || !globalThis.RegisterOfflineStore) { return; }
    const state = document.getElementById("offline-unlock-state");
    let prepared;
    let denied = false;
    let validationRun = 0;

    function tile(product) {
        const article = document.createElement("article");
        article.className = "product-tile";
        article.dataset.productId = product.id;
        const minus = document.createElement("button");
        minus.type = "button"; minus.className = "remove-one";
        minus.dataset.action = "minus"; minus.setAttribute("aria-label", `${product.name}を1個減らす`);
        minus.textContent = "−";
        const plus = document.createElement("button");
        plus.type = "button"; plus.className = "add-one";
        plus.dataset.action = "plus"; plus.setAttribute("aria-label", `${product.name}を1個追加`);
        const fallback = document.createElement("span");
        fallback.className = "no-image"; fallback.textContent = product.name;
        if (product.image && (/^\/static\/(register_images|product_images)\/[\w./-]+$/.test(product.image)
                || (/^data:image\/webp;base64,[A-Za-z0-9+/=]+$/.test(product.image) && product.image.length <= 683000))) {
            const img = document.createElement("img");
            img.src = product.image; img.alt = ""; img.width = 160; img.height = 160;
            img.addEventListener("error", () => { img.hidden = true; fallback.hidden = false; });
            fallback.hidden = true; plus.append(img);
        }
        const label = document.createElement("span"); label.className = "product-label";
        label.textContent = product.name;
        const price = document.createElement("strong"); price.className = "product-price";
        price.textContent = `${product.price}円`;
        plus.append(fallback, label, price);
        const quantity = document.createElement("label"); quantity.className = "quantity-label";
        quantity.append("個数 ");
        const input = document.createElement("input"); input.className = "quantity-input";
        input.type = "number"; input.inputMode = "numeric"; input.min = "-50"; input.max = "50";
        input.step = "1"; input.value = "0"; input.dataset.action = "quantity";
        input.setAttribute("aria-label", `${product.name}の個数`);
        quantity.append(input); article.append(minus, plus, quantity);
        return article;
    }

    async function revalidate() {
        if (!prepared || denied) { return; }
        const run = ++validationRun;
        if (!navigator.onLine) {
            globalThis.RegisterOfflineAuth = "offline";
            window.dispatchEvent(new Event("register-auth-change"));
            return;
        }
        globalThis.RegisterOfflineAuth = "checking";
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 3000);
        try {
            const response = await fetch(`/api/register/offline/session?grant_id=${encodeURIComponent(prepared.grant_id)}`,
                { credentials: "same-origin", cache: "no-store", signal: controller.signal });
            if (run !== validationRun) { return; }
            if (response.status === 401 || response.redirected) {
                denied = true; globalThis.RegisterOfflineAuth = "denied";
                window.dispatchEvent(new Event("register-auth-change"));
                state.textContent = "⚠ Admin認証を確認できません。オンラインで再ログインし、この画面を再読み込みしてPINで開いてください。端末の会計記録は保持しています。";
                return;
            }
            if (response.status === 403 || response.status === 404) {
                denied = true; globalThis.RegisterOfflineAuth = "denied";
                window.dispatchEvent(new Event("register-auth-change"));
                state.textContent = "⚠ 端末許可を確認できません。オンラインでAdminが再準備してください。端末の会計記録は保持しています。";
                return;
            }
            if (!response.ok) { globalThis.RegisterOfflineAuth = "offline"; return; }
            const result = await response.json();
            if (result.dataset_id !== prepared.dataset_id || !result.csrf_token) {
                denied = true; globalThis.RegisterOfflineAuth = "denied";
                window.dispatchEvent(new Event("register-auth-change"));
                state.textContent = "⚠ Datasetが一致しません。会計記録は端末に保持しています。";
                return;
            }
            document.querySelector('meta[name="csrf-token"]').content = result.csrf_token;
            globalThis.RegisterOfflineAuth = "ready";
            window.dispatchEvent(new Event("register-auth-change"));
            window.dispatchEvent(new Event("register-auth-ready"));
        } catch (_) {
            if (run === validationRun) { globalThis.RegisterOfflineAuth = "offline"; }
        } finally {
            clearTimeout(timeout);
            if (run === validationRun) { window.dispatchEvent(new Event("register-auth-change")); }
        }
    }
    form.addEventListener("submit", async event => {
        event.preventDefault();
        try {
            if (localStorage.getItem("bakery-register-offline-locked")) {
                throw Error("ログアウト後はオンラインでAdminが端末を再準備してください。");
            }
            prepared = await RegisterOfflineStore.load(document.getElementById("offline-pin").value);
            document.getElementById("offline-pin").value = "";
            if (!prepared?.scope || prepared.scope !== `${prepared.dataset_id}:admin`
                || !prepared.grant_id || !prepared.token || !prepared.catalog_proof
                || !Array.isArray(prepared.products) || !prepared.products.length) {
                throw Error("商品情報と端末許可が一致しません。オンラインで再準備してください。");
            }
            const today = RegisterCore.tokyoDay(new Date());
            if (Date.now() > Date.parse(prepared.expires_at)
                || ![prepared.business_date, prepared.next_business_date].includes(today)) {
                throw Error("承認済み営業日または端末利用期限を過ぎました。オンラインで再準備してください。");
            }
            const products = prepared.products.filter(product => product.is_active !== false);
            globalThis.RegisterOfflineAuth = "checking";
            document.getElementById("product-grid").replaceChildren(...products.map(tile));
            globalThis.RegisterPager?.mount(document.getElementById("product-grid"));
            document.getElementById("register-config").textContent = JSON.stringify({
                products, scope: prepared.scope, deviceId: prepared.device_id,
                saveEnabled: true, offlineMode: true,
                offlineGrant: prepared.token, approvedDays: [prepared.business_date, prepared.next_business_date],
                offlineExpiresAt: prepared.expires_at, catalogProof: prepared.catalog_proof,
                catalogFetchedAt: prepared.issued_at,
                postUrl: "/api/register/transactions", offlineSyncUrl: "/api/register/offline/sync",
                statusBase: "/api/register/transactions", recoveryUrl: "/api/register/recovery/cases",
            });
            form.hidden = true;
            document.getElementById("catalog-observation").textContent =
                `◷ 保存済み商品情報（${prepared.issued_at}）を使用しています。`;
            state.textContent = "✓ 端末内レジを開きました。";
            const script = document.createElement("script"); script.src = "/static/register.js";
            window.addEventListener("online", revalidate);
            window.addEventListener("focus", revalidate);
            document.head.append(script);
            void revalidate();
        } catch (error) {
            prepared = null;
            state.textContent = `⚠ ${error.message} 端末の元記録は削除していません。`;
        }
    });
})();
