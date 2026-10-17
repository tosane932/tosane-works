/* Admin explicitly prepares a browser profile while authenticated and online. */
(function () {
    "use strict";
    const form = document.getElementById("offline-setup");
    if (!form || !globalThis.RegisterOfflineStore) { return; }
    const config = JSON.parse(document.getElementById("register-config").textContent);
    const output = document.getElementById("offline-setup-state");
    form.addEventListener("submit", async event => {
        event.preventDefault();
        const pin = document.getElementById("offline-setup-pin").value;
        const button = form.querySelector("button");
        button.disabled = true;
        output.textContent = "端末を準備しています。";
        try {
            if (!navigator.onLine || !navigator.serviceWorker || !globalThis.indexedDB ||
                !crypto?.subtle || !globalThis.caches) {
                throw Error("この端末ではオフライン準備を完了できません。");
            }
            if (pin.length < 8) { throw Error("端末用PINは8文字以上にしてください。"); }
            const response = await fetch(config.offlinePrepareUrl, {
                method: "POST", credentials: "same-origin",
                headers: { "Content-Type": "application/json",
                           "X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content },
                body: JSON.stringify({ device_id: crypto.randomUUID(),
                    next_day: document.getElementById("offline-next-day").checked }),
            });
            const prepared = response.headers.get("Content-Type")?.includes("application/json")
                ? await response.json() : null;
            if (!response.ok || !prepared?.token || prepared.scope !== config.scope) {
                throw Error(prepared?.error || "Admin認証と端末準備を確認してください。");
            }
            const registration = await navigator.serviceWorker.register("/register/offline-sw.js",
                                                                         { scope: "/register/" });
            await navigator.serviceWorker.ready; // install caches shell, CSS and JS atomically.
            if (!registration.active) { throw Error("オフライン用の画面を保存できませんでした。"); }
            // Images are optional. Missing images render as text tiles.
            registration.active.postMessage({ type: "CACHE_REGISTER_IMAGES",
                urls: prepared.products.map(product => product.image).filter(Boolean) });
            await RegisterOfflineStore.save(prepared, pin);
            localStorage.removeItem("bakery-register-offline-locked");
            document.getElementById("offline-setup-pin").value = "";
            const link = document.createElement("a");
            link.href = "/register/offline";
            link.textContent = "準備済みの端末内レジを開く";
            output.replaceChildren("✓ 端末を準備しました。", link);
        } catch (error) {
            output.textContent = `⚠ ${error.message}。準備は完了していません。`;
        } finally { button.disabled = false; }
    });
})();
