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
        const firstPage = grid.querySelector(".product-page");
        const tile = grid.querySelector(".product-tile");
        const gridHeight = firstPage && tile ? tile.getBoundingClientRect().height * 3
            + number(getComputedStyle(firstPage).rowGap) * 2 + edges(grid)
            + Math.max(8, grid.offsetHeight - grid.clientHeight) : 44;
        const catalogNeed = edges(catalog) + gridHeight + number(getComputedStyle(catalog).rowGap)
            + Math.max(outer(catalog.querySelector(".section-heading")), outer(catalog.querySelector(".product-page-controls")));
        const checkoutNeed = edges(checkout) + 52 + Array.from(checkout.children)
            .filter(node => node.id !== "cart-lines").reduce((sum, node) => sum + outer(node), 0);
        const fit = room >= Math.max(catalogNeed, checkoutNeed) + 2;
        const height = `${viewport}px`;
        if (shell.style.getPropertyValue("--register-height") !== height) { shell.style.setProperty("--register-height", height); }
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
    window.visualViewport?.addEventListener("resize", schedule);
    window.addEventListener("scroll", schedule, {passive: true});
    window.addEventListener("register-catalog-layout", schedule);
    schedule();
})();
