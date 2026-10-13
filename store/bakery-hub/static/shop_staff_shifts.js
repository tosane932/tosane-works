document.addEventListener("DOMContentLoaded", () => {
    const selected = document.querySelector('.staff-shifts-days [aria-current="date"]');
    if (selected) {
        const strip = selected.parentElement;
        strip.scrollLeft = selected.offsetLeft - strip.offsetLeft - (strip.clientWidth - selected.clientWidth) / 2;
    }
    const form = document.getElementById("shift-form");
    if (!form) return;
    const status = document.getElementById("shift-status");
    const fields = document.getElementById("shift-time-fields");
    const inputs = fields.querySelectorAll("input");
    function update() {
        const untimed = status.value !== "work";
        fields.hidden = untimed;
        inputs.forEach(input => {
            input.disabled = untimed;
            if (input.type === "time") input.required = !untimed;
        });
    }
    status.addEventListener("change", update);
    update();
});

