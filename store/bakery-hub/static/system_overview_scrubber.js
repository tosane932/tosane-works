/* Optional chapter-navigation prototype. Remove this file, its markup and CSS block independently. */
(() => {
    "use strict";

    const scrubber = document.getElementById("overview-chapter-scrubber");
    const thumb = scrubber?.querySelector('[role="slider"]');
    const preview = scrubber?.querySelector("[data-scrubber-preview]");
    const chapters = [...document.querySelectorAll("main > [data-overview-chapter]")];
    const links = [...document.querySelectorAll("#overview-page-contents a[data-overview-link]")];
    const viewport = window.matchMedia("(max-width: 900px) and (pointer: coarse) and (min-height: 600px)");

    if (!scrubber || !thumb || !preview || chapters.length !== 9 || links.length !== 9
        || !document.body.classList.contains("overview-navigation-ready")) return;
    // Opt-in for a real-device review before deciding whether to adopt it.
    if (new URLSearchParams(window.location.search).get("chapter-scrubber") !== "preview") return;

    let currentIndex = links.findIndex(link => link.getAttribute("aria-current") === "location");
    if (currentIndex < 0) currentIndex = 0;
    let drag = null;

    function setThumb(index) {
        const travel = Math.max(0, scrubber.clientHeight - thumb.offsetHeight);
        thumb.style.top = `${travel * index / (chapters.length - 1)}px`;
        preview.style.top = `${thumb.offsetTop + thumb.offsetHeight / 2}px`;
        thumb.setAttribute("aria-valuenow", String(index + 1));
        thumb.setAttribute("aria-valuetext", links[index].textContent.trim());
        preview.textContent = links[index].textContent.trim();
    }

    function showPreview(index) {
        setThumb(index);
        preview.hidden = false;
    }

    function moveIndex(clientY) {
        const travel = Math.max(1, scrubber.clientHeight - thumb.offsetHeight);
        const progress = Math.max(0, Math.min(1,
            (clientY - drag.grabOffset - scrubber.getBoundingClientRect().top
             - thumb.offsetHeight / 2) / travel));
        return Math.round(progress * (chapters.length - 1));
    }

    function jumpTo(index, keepFocus = false) {
        currentIndex = index;
        setThumb(index);
        const target = chapters[index];
        if (window.location.hash !== `#${target.id}`) {
            window.history.pushState(null, "", `#${target.id}`);
        }
        target.scrollIntoView({block: "start", behavior: "auto"});
        window.requestAnimationFrame(() => {
            if (keepFocus) thumb.focus({preventScroll: true});
            else target.querySelector("h2").focus({preventScroll: true});
        });
    }

    function syncVisibility() {
        const enabled = viewport.matches && !document.querySelector("#overview-page-contents[aria-modal]");
        if (!enabled && drag) {
            drag = null;
            preview.hidden = true;
        }
        scrubber.hidden = !enabled;
        if (enabled) setThumb(currentIndex);
        else if (document.activeElement === thumb) {
            document.getElementById("overview-location-button").focus({preventScroll: true});
        }
    }

    thumb.addEventListener("pointerdown", event => {
        if (!event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) return;
        event.preventDefault();
        thumb.focus({preventScroll: true});
        thumb.setPointerCapture(event.pointerId);
        drag = {
            pointerId: event.pointerId,
            startY: event.clientY,
            grabOffset: event.clientY - (thumb.getBoundingClientRect().top + thumb.offsetHeight / 2),
            candidate: currentIndex,
        };
        showPreview(currentIndex);
    });

    thumb.addEventListener("pointermove", event => {
        if (!drag || event.pointerId !== drag.pointerId) return;
        drag.candidate = moveIndex(event.clientY);
        showPreview(drag.candidate);
    });

    function finishDrag(event, commit) {
        if (!drag || event.pointerId !== drag.pointerId) return;
        const index = drag.candidate;
        const moved = Math.abs(event.clientY - drag.startY) >= 12;
        drag = null;
        if (thumb.hasPointerCapture(event.pointerId)) thumb.releasePointerCapture(event.pointerId);
        preview.hidden = true;
        if (commit && moved) jumpTo(index);
        else setThumb(currentIndex);
    }

    thumb.addEventListener("pointerup", event => finishDrag(event, true));
    thumb.addEventListener("pointercancel", event => finishDrag(event, false));

    thumb.addEventListener("keydown", event => {
        let index = currentIndex;
        if (event.key === "ArrowDown" || event.key === "ArrowRight") index++;
        else if (event.key === "ArrowUp" || event.key === "ArrowLeft") index--;
        else if (event.key === "Home") index = 0;
        else if (event.key === "End") index = chapters.length - 1;
        else return;
        event.preventDefault();
        index = Math.max(0, Math.min(chapters.length - 1, index));
        showPreview(index);
        jumpTo(index, true);
    });
    thumb.addEventListener("blur", () => {
        if (!drag) preview.hidden = true;
    });
    document.addEventListener("overview:chapterchange", event => {
        currentIndex = event.detail.index;
        if (!drag && !scrubber.hidden) setThumb(currentIndex);
    });
    document.addEventListener("overview:contentschange", syncVisibility);
    window.addEventListener("resize", syncVisibility, {passive: true});
    viewport.addEventListener("change", syncVisibility);
    syncVisibility();
})();
