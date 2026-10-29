"use strict";

document.querySelectorAll("[data-waste-form]").forEach((form) => {
  const status = form.querySelector("[data-save-state]");
  const caution = form.querySelector("[data-waste-caution]");
  const confirmation = form.elements.namedItem("overage_confirm");
  const fields = [...form.querySelectorAll("input[name='made_quantity'], input[name='discarded_quantity'], textarea[name='note'], select[name='product_key']")];
  const editing = form.hasAttribute("data-waste-edit");
  const original = editing
    ? [form.dataset.savedMade, form.dataset.savedDiscarded, form.dataset.savedNote]
    : fields.map((field) => field.value);
  const initialStatus = status.textContent;
  const refresh = (userChanged = false) => {
    const changed = fields.some((field, index) => field.value !== original[index]);
    if (userChanged) status.textContent = changed ? "未保存の変更があります" : (editing ? "保存済み" : "未入力");
    const made = form.elements.namedItem("made_quantity").value;
    const discarded = form.elements.namedItem("discarded_quantity").value;
    const needsConfirmation = made !== "" && discarded !== "" && Number(discarded) > Number(made) && (!editing || changed);
    caution.hidden = !needsConfirmation;
    if (!needsConfirmation) confirmation.checked = false;
  };
  const onChange = (event) => {
    if (event.target.name === "made_quantity" || event.target.name === "discarded_quantity") confirmation.checked = false;
    refresh(true);
  };
  form.addEventListener("input", onChange);
  form.addEventListener("change", onChange);
  let sending = false;
  form.addEventListener("submit", (event) => {
    if (sending) { event.preventDefault(); return; }
    if (!form.checkValidity()) return;
    if (editing && fields.every((field, index) => field.value === original[index])) {
      event.preventDefault();
      status.textContent = "変更はありません";
      return;
    }
    if (!caution.hidden && !confirmation.checked) {
      event.preventDefault();
      status.textContent = "廃棄数を確認し、確認欄を選んでください";
      confirmation.focus();
      return;
    }
    sending = true;
    status.textContent = "保存中…";
    form.querySelector("button[type='submit']").disabled = true;
  });
  refresh();
});
