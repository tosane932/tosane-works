/* Progressively enhance the existing contents; keep the document readable without JS. */
(() => {
    "use strict";

    const reader = document.getElementById("overview-reader-navigation");
    const button = document.getElementById("overview-location-button");
    const label = document.getElementById("overview-current-chapter");
    const panel = document.getElementById("overview-page-contents");
    const backdrop = document.getElementById("overview-contents-backdrop");
    const chapters = [...document.querySelectorAll("main > [data-overview-chapter]")];
    const links = panel ? [...panel.querySelectorAll("a[data-overview-link]")] : [];
    const closeButton = panel?.querySelector("[data-overview-close]");

    if (!reader || !button || !label || !panel || !closeButton || !backdrop
        || !chapters.length || chapters.length !== links.length) {
        return;
    }

    let currentIndex = 0;
    let scheduled = false;
    let contentsOpen = false;
    let closeTimer = null;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const background = [...document.querySelector("main").children]
        .filter(element => element !== reader && element !== panel);
    background.push(...document.querySelectorAll(".app-sidebar, .app-navigation-open-button"));

    function syncButton() {
        button.setAttribute("aria-expanded", String(contentsOpen));
        button.setAttribute("aria-label", `現在地：${label.textContent}。目次を${contentsOpen ? "閉じる" : "開く"}`);
    }

    function setCurrent(index) {
        currentIndex = index;
        label.textContent = links[index].textContent;
        links.forEach((link, i) => {
            if (i === index) {
                link.setAttribute("aria-current", "location");
            } else {
                link.removeAttribute("aria-current");
            }
        });
        syncButton();
        updateOffset();
        document.dispatchEvent(new CustomEvent("overview:chapterchange", {detail: {index}}));
    }

    function updateCurrent() {
        // Only nine chapter positions: a huge chapter need not intersect the viewport.
        // rAF batches scroll events, including jumps, backward scroll and details changes.
        const readingLine = button.getBoundingClientRect().bottom + 16;
        let index = 0;
        chapters.forEach((chapter, i) => {
            if (chapter.getBoundingClientRect().top <= readingLine) {
                index = i;
            }
        });
        if (window.scrollY > 0
            && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2) {
            index = chapters.length - 1;
        }
        if (index !== currentIndex) {
            setCurrent(index);
        }
    }

    function scheduleUpdate() {
        if (scheduled) return;
        scheduled = true;
        window.requestAnimationFrame(() => {
            scheduled = false;
            updateCurrent();
        });
    }

    function updateOffset() {
        // Measure the real bar, including wrapped text, zoom and safe-area padding.
        const offset = Math.ceil(button.getBoundingClientRect().bottom + 12);
        document.body.style.setProperty("--overview-anchor-offset", `${offset}px`);
        scheduleUpdate();
    }

    function closeContents(returnFocus = false) {
        if (!contentsOpen) return;
        contentsOpen = false;
        document.body.classList.remove("overview-contents-open");
        document.body.style.overflow = "";
        background.forEach(element => { element.inert = false; });
        panel.inert = true;
        panel.removeAttribute("aria-modal");
        syncButton();
        document.dispatchEvent(new Event("overview:contentschange"));
        if (returnFocus) button.focus({preventScroll: true});
        clearTimeout(closeTimer);
        const finish = () => {
            if (contentsOpen) return;
            panel.hidden = true;
            backdrop.hidden = true;
        };
        if (reducedMotion.matches) finish();
        else closeTimer = window.setTimeout(finish, 240);
    }

    function openContents() {
        updateCurrent();
        clearTimeout(closeTimer);
        contentsOpen = true;
        panel.hidden = false;
        panel.inert = false;
        panel.setAttribute("aria-modal", "true");
        backdrop.hidden = false;
        background.forEach(element => { element.inert = true; });
        syncButton();
        document.dispatchEvent(new Event("overview:contentschange"));
        const current = links[currentIndex];
        current.focus({preventScroll: true});
        // Scroll only the panel: opening a late chapter must not move the document.
        panel.scrollTop += current.getBoundingClientRect().top
            - panel.getBoundingClientRect().top - panel.clientHeight / 2;
        // Keep the initial frame for opacity/translate to animate from.
        window.requestAnimationFrame(() => {
            if (contentsOpen) document.body.classList.add("overview-contents-open");
        });
    }

    button.addEventListener("click", () => {
        if (!contentsOpen) openContents();
        else closeContents(true);
    });
    closeButton.addEventListener("click", () => closeContents(true));
    backdrop.addEventListener("click", () => closeContents(true));

    links.forEach((link, index) => {
        link.addEventListener("click", event => {
            if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            closeContents();
            setCurrent(index);
            // Keep the native anchor/hash/history and CSS scroll margin. Move keyboard
            // focus to the heading after the browser has performed the anchor jump.
            window.requestAnimationFrame(() => {
                chapters[index].querySelector("h2").focus({preventScroll: true});
                scheduleUpdate();
            });
        });
    });

    reader.addEventListener("keydown", event => {
        if (!contentsOpen) return;
        if (event.key === "Escape") {
            event.preventDefault();
            closeContents(true);
        } else if (event.key === "Tab") {
            const focusables = [button, closeButton, ...links];
            if (event.shiftKey && document.activeElement === focusables[0]) {
                event.preventDefault();
                focusables.at(-1).focus();
            } else if (!event.shiftKey && document.activeElement === focusables.at(-1)) {
                event.preventDefault();
                focusables[0].focus();
            }
        }
    });
    window.addEventListener("scroll", scheduleUpdate, {passive: true});
    window.addEventListener("resize", updateOffset, {passive: true});
    window.addEventListener("hashchange", scheduleUpdate);
    window.addEventListener("pageshow", scheduleUpdate);
    window.addEventListener("load", scheduleUpdate);
    document.addEventListener("toggle", scheduleUpdate, true);

    // Reuse one list instead of rendering duplicate desktop/mobile contents.
    panel.hidden = true;
    reader.append(panel);
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-labelledby", "overview-contents-title");
    closeButton.hidden = false;
    reader.hidden = false;
    document.body.classList.add("overview-navigation-ready");
    setCurrent(0);
    if ("ResizeObserver" in window) {
        new ResizeObserver(updateOffset).observe(button);
    }
})();
