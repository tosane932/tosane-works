/* Progressively enhance the existing contents; keep the document readable without JS. */
(() => {
    "use strict";

    const reader = document.getElementById("overview-reader-navigation");
    const button = document.getElementById("overview-location-button");
    const label = document.getElementById("overview-current-chapter");
    const panel = document.getElementById("overview-page-contents");
    const chapters = [...document.querySelectorAll("main > [data-overview-chapter]")];
    const links = panel ? [...panel.querySelectorAll("a[data-overview-link]")] : [];
    const closeButton = panel?.querySelector("[data-overview-close]");

    if (!reader || !button || !label || !panel || !closeButton
        || !chapters.length || chapters.length !== links.length) {
        return;
    }

    let currentIndex = 0;
    let scheduled = false;

    function syncButton() {
        button.setAttribute("aria-expanded", String(!panel.hidden));
        button.setAttribute("aria-label", `現在地：${label.textContent}。目次を${panel.hidden ? "開く" : "閉じる"}`);
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
        panel.hidden = true;
        syncButton();
        if (returnFocus) button.focus({preventScroll: true});
    }

    function openContents() {
        updateCurrent();
        panel.hidden = false;
        syncButton();
        const current = links[currentIndex];
        current.focus({preventScroll: true});
        // Scroll only the panel: opening a late chapter must not move the document.
        panel.scrollTop += current.getBoundingClientRect().top
            - panel.getBoundingClientRect().top - panel.clientHeight / 2;
    }

    button.addEventListener("click", () => {
        if (panel.hidden) openContents();
        else closeContents(true);
    });
    closeButton.addEventListener("click", () => closeContents(true));

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

    document.addEventListener("click", event => {
        if (!panel.hidden && !reader.contains(event.target)) closeContents();
    });
    reader.addEventListener("keydown", event => {
        if (event.key === "Escape" && !panel.hidden) {
            event.preventDefault();
            closeContents(true);
        }
    });
    reader.addEventListener("focusout", event => {
        // A disclosure is not a modal: Tab can leave it without trapping focus.
        if (!reader.contains(event.relatedTarget)) closeContents();
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
    closeButton.hidden = false;
    reader.hidden = false;
    document.body.classList.add("overview-navigation-ready");
    setCurrent(0);
    if ("ResizeObserver" in window) {
        new ResizeObserver(updateOffset).observe(button);
    }
})();
