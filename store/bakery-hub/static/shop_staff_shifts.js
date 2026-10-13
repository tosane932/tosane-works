document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("shift-form");
    if (!form) return;
    const status = document.getElementById("shift-status");
    const fields = document.getElementById("shift-time-fields");
    const inputs = fields.querySelectorAll("input");
    function update() {
        const off = status.value === "off";
        fields.hidden = off;
        inputs.forEach(input => {
            input.disabled = off;
            if (input.type === "time") input.required = !off;
        });
    }
    status.addEventListener("change", update);
    update();
});
