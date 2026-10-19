/* Phone: one vertical list. Tablet: continuous groups of 12. Neither owns checkout state. */
(function (root) {
    "use strict";
    const PAGE_SIZE = 12;
    function group(items) {
        const pages = [];
        for (let i = 0; i < items.length; i += PAGE_SIZE) { pages.push(items.slice(i, i + PAGE_SIZE)); }
        return pages;
    }
    function emptySlots(count) { return count ? (PAGE_SIZE - count % PAGE_SIZE) % PAGE_SIZE : 0; }
    const mounted = new WeakMap();
    function mount(container) {
        if (!container) { return; }
        if (mounted.has(container)) { mounted.get(container).refresh(); return; }
        const previous = document.getElementById("products-previous");
        const next = document.getElementById("products-next");
        const position = document.getElementById("products-position");
        const progress = document.getElementById("products-progress");
        const controls = document.querySelector(".product-page-controls");
        const hint = document.getElementById("products-swipe-hint");
        const phone = root.matchMedia("(max-width: 850px)");
        let phoneMode;
        let desktopLeft = 0;
        let pages = [];
        let tiles = [];
        function display() {
            if (phoneMode) { position.textContent = `全${tiles.length}商品`; return; }
            const viewport = container.getBoundingClientRect();
            const visible = tiles.map((tile, index) => ({ index, box: tile.getBoundingClientRect() }))
                .filter(({ box }) => box.right > viewport.left + 4 && box.left < viewport.right - 4);
            position.textContent = visible.length ?
                `${visible[0].index + 1}–${visible[visible.length - 1].index + 1} / ${tiles.length}商品` : `0 / ${tiles.length}商品`;
            const max = Math.max(0, container.scrollWidth - container.clientWidth);
            previous.disabled = container.scrollLeft <= 1;
            next.disabled = container.scrollLeft >= max - 1;
            progress.value = max ? Math.min(100, 100 * container.scrollLeft / max) : (tiles.length ? 100 : 0);
        }
        function refresh() {
            if (phoneMode === false) { desktopLeft = container.scrollLeft; }
            const focused = document.activeElement;
            tiles = Array.from(container.querySelectorAll(".product-tile"));
            phoneMode = phone.matches;
            pages = phoneMode ? [] : group(tiles).map((items, index, all) => {
                const node = document.createElement("div");
                node.className = "product-page product-grid";
                node.setAttribute("role", "group");
                node.setAttribute("aria-label", `商品グループ ${index + 1}`);
                node.append(...items);
                if (index === all.length - 1) {
                    for (let i = 0; i < emptySlots(items.length); i++) {
                        const empty = document.createElement("div");
                        empty.className = "product-placeholder";
                        empty.setAttribute("aria-hidden", "true");
                        empty.textContent = "未登録";
                        node.append(empty);
                    }
                }
                return node;
            });
            container.replaceChildren(...(phoneMode ? tiles : pages));
            container.classList.toggle("product-grid", phoneMode);
            container.classList.toggle("product-pages", !phoneMode);
            container.setAttribute("aria-label", phoneMode ? "商品一覧（2列・縦スクロール）" : "商品一覧（横スクロール）");
            controls.hidden = phoneMode;
            hint.textContent = phoneMode ? "タップで1個追加・下へスクロール" : "タップで1個追加・左右にスクロール";
            if (!tiles.length) {
                const empty = document.createElement("p");
                empty.textContent = "販売中の商品はありません。商品一覧で登録・販売再開してください。";
                container.append(empty);
            }
            container.scrollLeft = phoneMode ? 0 : desktopLeft;
            if (focused && container.contains(focused)) { focused.focus({ preventScroll: true }); }
            display();
            root.dispatchEvent(new Event("register-catalog-layout"));
        }
        function move(direction, amount = container.clientWidth * 0.8) {
            if (phoneMode) { return; }
            container.scrollBy({ left: direction * amount, behavior: "auto" });
            display();
        }
        previous.addEventListener("click", () => move(-1));
        next.addEventListener("click", () => move(1));
        container.addEventListener("keydown", event => {
            if (phoneMode) { return; }
            if (event.target.matches("input,select,textarea")) { return; }
            if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                event.preventDefault();
                move(event.key === "ArrowLeft" ? -1 : 1, (tiles[0]?.getBoundingClientRect().width || 160) + 10);
            }
        });
        container.addEventListener("scroll", display, { passive: true });
        let origin;
        let moved = false;
        container.addEventListener("pointerdown", event => {
            origin = [event.clientX, event.clientY]; moved = false;
        }, { passive: true });
        container.addEventListener("pointermove", event => {
            if (origin && Math.hypot(event.clientX - origin[0], event.clientY - origin[1]) > 10) { moved = true; }
        }, { passive: true });
        container.addEventListener("pointercancel", () => { moved = true; origin = null; }, { passive: true });
        container.addEventListener("pointerup", () => { origin = null; }, { passive: true });
        container.addEventListener("click", event => {
            if (moved && event.detail !== 0) { event.preventDefault(); event.stopImmediatePropagation(); }
        }, true);
        if (typeof ResizeObserver !== "undefined") {
            new ResizeObserver(display).observe(container);
        }
        phone.addEventListener("change", refresh);
        mounted.set(container, { refresh });
        refresh();
    }
    const api = { PAGE_SIZE, group, emptySlots, mount };
    if (typeof module !== "undefined" && module.exports) { module.exports = api; }
    else {
        root.RegisterPager = api;
        mount(document.getElementById("product-grid"));
    }
})(globalThis);
