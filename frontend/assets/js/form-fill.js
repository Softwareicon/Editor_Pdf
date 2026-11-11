// Fill the PDF's own form fields (AcroForm). Fields are listed in a dialog; saving writes a new version.
const $ = (id) => document.getElementById(id);

export function setUpFormFill({ state, api, toast, edit, beforeOpen }) {
  const dialog = $("form-dialog"), host = $("form-fields");
  let fields = [];

  function control(field, i) {
    const id = `ff-${i}`;
    let input;
    if (field.type === "checkbox") {
      input = Object.assign(document.createElement("input"), { id, type: "checkbox", className: "form-check-input", checked: Boolean(field.value) });
    } else if (field.type === "choice" || field.type === "radio") {
      input = Object.assign(document.createElement("select"), { id, className: "form-select form-select-sm" });
      const options = field.options.includes(field.value) || !field.value ? field.options : [field.value, ...field.options];
      for (const text of field.type === "radio" || !field.value ? ["", ...options] : options) input.add(new Option(text || "—", text));
      input.value = field.value || "";
    } else {
      input = Object.assign(document.createElement(field.type === "multiline" ? "textarea" : "input"), { id, className: "form-control form-control-sm", value: field.value || "" });
      if (field.type === "multiline") input.rows = 3; else input.type = "text";
    }
    const label = Object.assign(document.createElement("label"), { htmlFor: id, className: "form-label mb-1", textContent: field.name });
    const row = Object.assign(document.createElement("div"), { className: field.type === "checkbox" ? "form-row check" : "form-row" });
    row.append(...(field.type === "checkbox" ? [input, label] : [label, input]));
    return row;
  }

  $("edit-form").addEventListener("click", async () => {
    if (!(await beforeOpen())) return;
    try {
      fields = (await api(`/api/tools/form/${encodeURIComponent(state.docId)}`)).fields;
    } catch (e) { return toast(e.message); }
    if (!fields.length) return toast("This PDF has no fillable form fields. Use the Text tool to type on it and ✓ / ✗ for check boxes.");
    host.replaceChildren(...fields.map(control));
    dialog.returnValue = "cancel";
    dialog.showModal();
  });

  dialog.addEventListener("close", () => {
    if (dialog.returnValue !== "ok") return;
    const values = {};
    fields.forEach((field, i) => {
      const input = $(`ff-${i}`);
      const value = field.type === "checkbox" ? input.checked : input.value;
      if (value !== (field.type === "checkbox" ? Boolean(field.value) : field.value || "")) values[field.name] = value;
    });
    const count = Object.keys(values).length;
    if (!count) return toast("Nothing was changed", "ok");
    edit("Filling the form", () => api("/api/tools/form", { method: "POST", json: { document_id: state.docId, values } }))
      .then((done) => { if (done) toast(`Filled ${count} field${count === 1 ? "" : "s"}. Use Undo to take it back.`, "ok"); });
  });
}
