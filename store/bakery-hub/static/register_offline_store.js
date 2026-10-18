/* PIN-wrapped device preparation. Receipts remain in the existing queue store. */
(function (root) {
    "use strict";
    const encoder = new TextEncoder();
    const decoder = new TextDecoder();
    const tag = encoder.encode("bakery-register-offline-v1");
    const bytes = value => Uint8Array.from(atob(value), char => char.charCodeAt(0));
    function base64(value) {
        // A photo catalog can exceed the JS argument limit; encode bounded chunks.
        const buffer = new Uint8Array(value);
        const chunks = [];
        for (let offset = 0; offset < buffer.length; offset += 8192) {
            chunks.push(String.fromCharCode(...buffer.subarray(offset, offset + 8192)));
        }
        return btoa(chunks.join(""));
    }

    function open() {
        return new Promise((resolve, reject) => {
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
            request.onblocked = () => reject(Error("他のレジ画面を閉じてから端末を準備してください。"));
        });
    }
    function operation(db, mode, action) {
        return new Promise((resolve, reject) => {
            const tx = db.transaction("offline", mode);
            const request = action(tx.objectStore("offline"));
            let value;
            request.onsuccess = () => { value = request.result; };
            tx.oncomplete = () => resolve(value);
            tx.onerror = () => reject(tx.error || Error("端末保存に失敗しました。"));
            tx.onabort = () => reject(tx.error || Error("端末保存を完了できませんでした。"));
        });
    }
    async function key(pin, salt) {
        if (typeof pin !== "string" || pin.length < 8 || !crypto.subtle) {
            throw Error("端末用PINを8文字以上で入力してください。");
        }
        const material = await crypto.subtle.importKey("raw", encoder.encode(pin), "PBKDF2", false, ["deriveKey"]);
        return crypto.subtle.deriveKey(
            { name: "PBKDF2", salt, iterations: 310000, hash: "SHA-256" },
            material, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
    }
    async function save(preparation, pin) {
        const salt = crypto.getRandomValues(new Uint8Array(16));
        const iv = crypto.getRandomValues(new Uint8Array(12));
        const cipher = await crypto.subtle.encrypt(
            { name: "AES-GCM", iv, additionalData: tag },
            await key(pin, salt), encoder.encode(JSON.stringify(preparation)));
        const db = await open();
        try {
            await operation(db, "readwrite", store =>
                store.put({ id: "active", salt: base64(salt), iv: base64(iv), data: base64(cipher) }));
        } finally { db.close?.(); }
    }
    async function load(pin) {
        const db = await open();
        let record;
        try { record = await operation(db, "readonly", store => store.get("active")); }
        finally { db.close?.(); }
        if (!record) { throw Error("この端末は準備されていません。オンラインでAdminが準備してください。"); }
        try {
            const plain = await crypto.subtle.decrypt(
                { name: "AES-GCM", iv: bytes(record.iv), additionalData: tag },
                await key(pin, bytes(record.salt)), bytes(record.data));
            return JSON.parse(decoder.decode(plain));
        } catch (_) { throw Error("端末用PINまたは保存済み情報を確認できません。"); }
    }
    root.RegisterOfflineStore = { open, save, load };
})(globalThis);
