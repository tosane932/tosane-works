/* 確認は表示だけ。認可・検証・二重送信防止はサーバーで行う。 */
(function () {
    "use strict";
    const form = document.getElementById("product-editor");
    if (!form) { return; }
    const dialog = document.getElementById("product-confirm");
    const preview = document.getElementById("product-photo-preview");
    const initial = preview.getAttribute("src");
    let previewUrl;
    let confirmed = false;
    form.addEventListener("change", event => {
        const el = event.target;
        if (!["photo", "camera_photo", "image_key"].includes(el.name)) { return; }
        if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
        const upload = form.elements.photo;
        const camera = form.elements.camera_photo;
        if (el.name === "photo" && upload?.files.length && camera) { camera.value = ""; }
        if (el.name === "camera_photo" && camera?.files.length && upload) { upload.value = ""; }
        if (el.name === "image_key") {
            if (upload) { upload.value = ""; } if (camera) { camera.value = ""; }
        }
        const file = upload?.files[0] || camera?.files[0];
        const select = form.elements.image_key;
        const source = file ? (previewUrl = URL.createObjectURL(file)) :
            (select.value === "__keep__" ? initial : select.selectedOptions[0].dataset.imageUrl);
        preview.hidden = !source;
        if (source) { preview.src = source; } else { preview.removeAttribute("src"); }
    });
    form.addEventListener("submit", event => {
        if (confirmed || typeof dialog.showModal !== "function") { return; }
        event.preventDefault();
        const el = form.elements;
        document.getElementById("product-confirm-summary").textContent =
            `${el.name.value}\n販売価格：${Number(el.price_yen.value).toLocaleString("ja-JP")}円\nパンの種類：${el.bread_kind.selectedOptions[0].text}\n商品分類：${el.product_class.selectedOptions[0].text}\n写真：${preview.hidden ? "写真なし（文字タイル）" : "選択した写真"}`;
        dialog.showModal();
    });
    document.getElementById("product-confirm-back").addEventListener("click", () => dialog.close());
    document.getElementById("product-confirm-save").addEventListener("click", () => {
        confirmed = true; dialog.close(); form.requestSubmit();
    });
})();
