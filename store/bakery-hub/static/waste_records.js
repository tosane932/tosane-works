"use strict";

document.querySelectorAll("[data-waste-form]").forEach((form) => {
  const status = form.querySelector("[data-save-state]");
  const caution = form.querySelector("[data-waste-caution]");
  const fields = [...form.querySelectorAll("input[name='made_quantity'], input[name='discarded_quantity'], textarea[name='note'], select[name='product_key']")];
  const original = fields.map((field) => field.value);
  const initialStatus = status.textContent;
  const refresh = () => {
    const changed = fields.some((field, index) => field.value !== original[index]);
    status.textContent = changed ? "未保存の変更があります" : initialStatus;
    const made = form.elements.namedItem("made_quantity").value;
    const discarded = form.elements.namedItem("discarded_quantity").value;
    caution.hidden = !(made !== "" && discarded !== "" && Number(discarded) > Number(made));
  };
  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);
  let sending = false;
  form.addEventListener("submit", (event) => {
    if (sending) { event.preventDefault(); return; }
    if (!form.checkValidity()) return;
    sending = true;
    status.textContent = "保存中…";
    form.querySelector("button[type='submit']").disabled = true;
  });
  refresh();
});
