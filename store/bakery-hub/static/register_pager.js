/* Native horizontal scroll + snap. The pager never reads or changes checkout state. */
(function (root) {
    "use strict";
    const PAGE_SIZE = 12;
    function group(items) {
        const pages = [];
        for (let i = 0; i < items.length; i += PAGE_SIZE) { pages.push(items.slice(i, i + PAGE_SIZE)); }
        return pages;
    }
    const mounted = new WeakMap();
    function mount(container) {
        if (!container) { return; }
        if (mounted.has(container)) { mounted.get(container).refresh(); return; }
        const previous = document.getElementById("products-previous");
        const next = document.getElementById("products-next");
        const position = document.getElementById("products-position");
        const dots = document.getElementById("products-dots");
        let page = 0;
        let pages = [];
        function display() {
            page = Math.max(0, Math.min(page, pages.length - 1));
            position.textContent = pages.length ? `${page + 1} / ${pages.length}` : "0 / 0";
            dots.textContent = pages.map((_, index) => index === page ? "●" : "○").join(" ");
            previous.disabled = page === 0;
            next.disabled = page >= pages.length - 1;
            pages.forEach((node, index) => { node.inert = index !== page; });
        }
        function refresh() {
            const tiles = Array.from(container.querySelectorAll(".product-tile"));
            pages = group(tiles).map((items, index) => {
                const node = document.createElement("div");
                node.className = "product-page product-grid";
                node.setAttribute("role", "group");
                node.setAttribute("aria-label", `商品ページ ${index + 1}`);
                node.append(...items);
                return node;
            });
            container.replaceChildren(...pages);
            container.classList.remove("product-grid");
            container.classList.add("product-pages");
            if (!pages.length) {
                const empty = document.createElement("p");
                empty.textContent = "販売中の商品はありません。商品一覧で登録・販売再開してください。";
                container.append(empty);
            }
            display();
            container.scrollLeft = page * container.clientWidth;
        }
        function go(value) {
            page = Math.max(0, Math.min(value, pages.length - 1));
            container.scrollTo({ left: page * container.clientWidth, behavior: "auto" });
            display();
        }
        previous.addEventListener("click", () => go(page - 1));
        next.addEventListener("click", () => go(page + 1));
        container.addEventListener("keydown", event => {
            if (event.target.matches("input,select,textarea")) { return; }
            if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                event.preventDefault(); go(page + (event.key === "ArrowLeft" ? -1 : 1));
            }
        });
        container.addEventListener("scroll", () => {
            if (container.clientWidth) { page = Math.round(container.scrollLeft / container.clientWidth); display(); }
        }, { passive: true });
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
            new ResizeObserver(() => { container.scrollLeft = page * container.clientWidth; }).observe(container);
        }
        mounted.set(container, { refresh });
        refresh();
    }
    const api = { PAGE_SIZE, group, mount };
    if (typeof module !== "undefined" && module.exports) { module.exports = api; }
    else {
        root.RegisterPager = api;
        mount(document.getElementById("product-grid"));
    }
})(globalThis);
