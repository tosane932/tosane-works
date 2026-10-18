/* Dedicated shell only. Never cache authenticated register HTML, cookies or API responses. */
const VERSION = "register-offline-v3-product-tile";
const SHELL = "/register/offline";
const ASSET_PATHS = [ "/static/register.css", "/static/register_core.js",
                "/static/register.js", "/static/register_cash_pad.js", "/static/register_pager.js", "/static/register_offline_store.js",
                "/static/register_offline_boot.js"];
const ASSETS = ASSET_PATHS.map(path => `${path}?v=product-tile-3`);

self.addEventListener("install", event => {
    event.waitUntil((async () => {
        const shell = await fetch(SHELL, { credentials: "same-origin", cache: "no-store" });
        if (!shell.ok || shell.redirected || shell.headers.get("X-Register-Offline-Shell") !== "1") {
            throw Error("Admin shell was not authorized");
        }
        const cache = await caches.open(VERSION);
        await cache.addAll(ASSETS);
        await cache.put(SHELL, shell);
    })());
});
self.addEventListener("activate", event => {
    // Do not discard older app assets while receipts in IndexedDB may depend on them.
    event.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", event => {
    const url = new URL(event.request.url);
    if (event.request.method !== "GET" || url.origin !== self.location.origin) { return; }
    if ((url.pathname === SHELL || url.pathname === "/register/preview")
        && event.request.mode === "navigate") {
        event.respondWith(fetch(event.request).then(response =>
            response.status >= 500 ? caches.open(VERSION).then(cache => cache.match(SHELL)) : response)
            .catch(() => caches.open(VERSION).then(cache => cache.match(SHELL))));
    } else if (ASSET_PATHS.includes(url.pathname) || url.pathname.startsWith("/static/register_images/")
               || url.pathname.startsWith("/static/product_images/")) {
        event.respondWith(caches.open(VERSION).then(cache => cache.match(event.request)).then(cached => cached || fetch(event.request)));
    }
    // Other navigations, status queries, CSRF and write APIs are never cached.
});
self.addEventListener("message", event => {
    if (event.data?.type !== "CACHE_REGISTER_IMAGES" || !Array.isArray(event.data.urls)) { return; }
    const urls = event.data.urls.filter(path =>
        typeof path === "string" && (path.startsWith("/static/register_images/")
            || path.startsWith("/static/product_images/")));
    event.waitUntil(caches.open(VERSION).then(async cache => {
        await Promise.allSettled(urls.map(path => cache.add(path)));
        event.ports[0]?.postMessage({ ready: true });
    }));
});
