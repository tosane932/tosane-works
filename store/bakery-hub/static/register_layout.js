/* Viewport and navigation only. Never read or write cart, receipts or browser storage. */
(function () {
    "use strict";
    const shell = document.getElementById("register-root");
    if (!shell) { return; }
    const layout = shell.querySelector(".register-layout");
    const catalog = shell.querySelector(".catalog");
    const checkout = shell.querySelector(".checkout");
    const payment = document.getElementById("checkout-payment");
    const grid = document.getElementById("product-grid");
    const jump = document.getElementById("jump-checkout");
    const back = document.getElementById("return-products");
    const notice = document.getElementById("register-layout-notice");
    const phone = matchMedia("(max-width: 850px)");
    let productTop = 0;
    let productFocus;
    let frame;
    const visible = node => node && getComputedStyle(node).display !== "none";
    const number = value => parseFloat(value) || 0;
    function outer(node) {
        if (!visible(node)) { return 0; }
        const css = getComputedStyle(node);
        return node.getBoundingClientRect().height + number(css.marginTop) + number(css.marginBottom);
    }
    function edges(node) {
        const css = getComputedStyle(node);
        return number(css.paddingTop) + number(css.paddingBottom) + number(css.borderTopWidth) + number(css.borderBottomWidth);
    }
    function setSize(name, value) {
        if (shell.style.getPropertyValue(name) !== value) { shell.style.setProperty(name, value); }
    }
    function update() {
        frame = null;
        const viewport = Math.min(innerHeight, window.visualViewport?.height || innerHeight);
        if (phone.matches) {
            document.body.classList.remove("register-fit");
            if (!notice.hidden) { notice.hidden = true; }
            const box = payment.getBoundingClientRect();
            const hide = box.top < viewport - 90 && box.bottom > 0;
            if (jump.hidden !== hide) { jump.hidden = hide; }
            return;
        }
        if (!jump.hidden) { jump.hidden = true; }
        const outside = Array.from(shell.children).filter(node =>
            node !== layout && node !== jump && node !== notice && visible(node));
        const room = viewport - edges(shell) - outside.reduce((sum, node) => sum + outer(node), 0)
            - number(getComputedStyle(shell).rowGap) * outside.length;
        // Intrinsic children only: never measure stretched grid/cart heights to decide fit.
        const checkoutNeed = () => edges(checkout) + 44 + number(getComputedStyle(document.getElementById("cart-lines")).marginBottom) + Array.from(checkout.children)
            .filter(node => node.id !== "cart-lines").reduce((sum, node) => sum + outer(node), 0);
        setSize("--cash-key-height", "60px");
        const preferredNeed = checkoutNeed();
        const keyHeight = Math.max(52, Math.min(60, Math.floor(60 - (preferredNeed - room + 2) / 4)));
        setSize("--cash-key-height", `${keyHeight}px`);
        const catalogCSS = getComputedStyle(catalog);
        const overhead = edges(catalog) + edges(grid) + 12 + number(catalogCSS.rowGap)
            + Math.max(outer(catalog.querySelector(".section-heading")), outer(catalog.querySelector(".product-page-controls")));
        const gap = 10;
        const available = Math.max(0, room - overhead - 4);
        // Touch tablets favour photos and two rows. Fine pointers can use 3–4 rows
        // only when the actual available height can keep every tile readable.
        const minTile = Math.max(168, number(getComputedStyle(document.documentElement).fontSize) * 10.5);
        const touch = matchMedia("(pointer: coarse)").matches;
        const rows = touch ? 2 : Math.max(2, Math.min(4, Math.floor((available + gap) / (minTile + gap))));
        const columns = touch ? 2 : 3;
        const width = Math.max(minTile, (grid.clientWidth - 6 - gap * (columns - 1)) / columns);
        const tileSize = Math.max(minTile, Math.floor(Math.min((available - gap * (rows - 1)) / rows, width)));
        setSize("--tile-size", `${tileSize}px`);
        setSize("--product-rows", String(rows));
        window.RegisterPager?.setRows(grid, rows);
        const catalogNeed = grid.querySelector(".product-tile") ? overhead + tileSize * rows + gap * (rows - 1) : overhead + 44;
        const fit = room >= Math.max(catalogNeed, checkoutNeed()) + 2;
        setSize("--register-height", `${viewport}px`);
        document.body.classList.toggle("register-fit", fit);
        if (notice.hidden !== fit) { notice.hidden = fit; }
    }
    function schedule() {
        if (!frame) { frame = requestAnimationFrame(update); }
    }
    jump.addEventListener("click", () => {
        productTop = scrollY;
        payment.scrollIntoView({block: "start", behavior: "instant"});
        const target = visible(document.getElementById("cash-area")) ? document.getElementById("cash-keypad") : back;
        target.focus({preventScroll: true});
        update();
    });
    grid.addEventListener("focusin", event => { productFocus = event.target; });
    back.addEventListener("click", () => {
        window.scrollTo({top: productTop, behavior: "instant"});
        (productFocus?.isConnected ? productFocus : grid).focus({preventScroll: true});
        update();
    });
    const tools = document.getElementById("register-tools-dialog");
    document.getElementById("show-register-tools")?.addEventListener("click", () => tools.showModal());
    document.getElementById("close-register-tools")?.addEventListener("click", () => tools.close());
    // Measure content, not a guessed fixed viewport offset. Oversized warnings/text use normal document flow.
    if (typeof ResizeObserver !== "undefined") {
        const observer = new ResizeObserver(schedule);
        for (const node of [...shell.children, payment, ...checkout.children]) {
            if (node !== layout && node !== jump && node !== notice) { observer.observe(node); }
        }
    }
    new MutationObserver(schedule).observe(shell, {subtree: true, childList: true, characterData: true,
        attributes: true, attributeFilter: ["hidden"]});
    window.addEventListener("resize", schedule);
    matchMedia("(pointer: coarse)").addEventListener("change", schedule);
    window.visualViewport?.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, {passive: true});
    window.addEventListener("register-catalog-layout", schedule);
    schedule();
})();
