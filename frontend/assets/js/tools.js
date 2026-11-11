// PDF tools page. Each tool is described by a small config: its form fields and how to call the API.
import { api, formatBytes, logout, requireUser, toast } from "./api.js";

const $ = (id) => document.getElementById(id);
function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

const POSITIONS = [["bottom-center", "Bottom centre"], ["bottom-right", "Bottom right"], ["bottom-left", "Bottom left"],
                   ["top-center", "Top centre"], ["top-right", "Top right"], ["top-left", "Top left"]];
const PAGES = { name: "pages", label: 'Pages ("all" or e.g. 1-3, 5)', type: "text", value: "all" };
const post = (path, json) => api(`/api/tools/${path}`, { method: "POST", json });
const opened = (doc, text = "Done.") => ({ text, docs: [doc] });

const TOOLS = [
  { id: "edit", title: "Edit PDF", desc: "Add or change text, whiteout, images, shapes, links and signatures.", fields: [{ name: "document_id", type: "doc" }],
    button: "Open editor", run: (v) => { location.href = `editor.html?id=${encodeURIComponent(v.document_id)}`; return new Promise(() => {}); } },
  { id: "merge", title: "Merge PDF", desc: "Join several PDFs into one, in the order you tick them.",
    fields: [{ name: "document_ids", type: "docs" }, { name: "name", label: "Name of the new file", type: "text", value: "Merged" }],
    run: async (v) => opened(await post("merge", v), "Merged into a new document.") },
  { id: "split", title: "Split PDF", desc: "Cut a PDF into several documents.",
    fields: [{ name: "document_id", type: "doc" },
             { name: "mode", label: "How", type: "select", options: [["ranges", "By page groups"], ["every", "Every N pages"]] },
             { name: "ranges", label: "Page groups, one new file each (e.g. 1-3, 4-6, 9)", type: "text", value: "" },
             { name: "every", label: "N (used with “Every N pages”)", type: "number", value: 1, min: 1 }],
    run: async (v) => { const r = await post("split", v); return { text: `Created ${r.documents.length} documents.`, docs: r.documents }; } },
  { id: "compress", title: "Compress PDF", desc: "Make a smaller copy. “Lossless” keeps pictures untouched; the others re-encode them.",
    fields: [{ name: "document_id", type: "doc" },
             { name: "level", label: "Compression", type: "select", options: [["balanced", "Balanced"], ["strong", "Strong (smaller, lower picture quality)"], ["lossless", "Lossless"]] }],
    run: async (v) => {
      const r = await post("compress", v);
      if (!r.document) return { text: `This file is already compact (${formatBytes(r.before)}); no copy was made.` };
      return { text: `${formatBytes(r.before)} → ${formatBytes(r.after)} (${Math.round((1 - r.after / r.before) * 100)}% smaller).`, docs: [r.document] };
    } },
  { id: "images-to-pdf", title: "JPG / PNG to PDF", desc: "Turn pictures into a PDF, one picture per page.",
    fields: [{ name: "files", label: "Pictures (up to 30)", type: "files", accept: "image/png,image/jpeg,image/webp,image/gif" },
             { name: "page_size", label: "Page size", type: "select", options: [["a4", "A4 with margins"], ["fit", "Same size as each picture"]] },
             { name: "name", label: "Name of the new file", type: "text", value: "Images" }],
    run: async (v) => {
      const form = new FormData();
      for (const f of v.files) form.append("files", f);
      form.append("page_size", v.page_size); form.append("name", v.name || "Images");
      return opened(await api("/api/tools/images-to-pdf", { method: "POST", body: form }), "Created a new document.");
    } },
  { id: "to-images", title: "PDF to JPG / PNG", desc: "Download every page as a picture (ZIP file, up to 100 pages).",
    fields: [{ name: "document_id", type: "doc" },
             { name: "format", label: "Format", type: "select", options: [["jpg", "JPG"], ["png", "PNG"]] },
             { name: "dpi", label: "Quality", type: "select", options: [["150", "Normal (150 dpi)"], ["72", "Screen (72 dpi)"], ["300", "Print (300 dpi)"]] }],
    run: (v) => download("to-images", { ...v, dpi: Number(v.dpi) }) },
  { id: "to-text", title: "PDF to text", desc: "Download the document's text as a .txt file. Scanned pages have no text.",
    fields: [{ name: "document_id", type: "doc" }], run: (v) => download("to-text", v) },
  { id: "numbers", title: "Page numbers", desc: "Number the pages. {n} is the page number, {total} the number of pages.",
    fields: [{ name: "document_id", type: "doc" }, { name: "text", label: "Text", type: "text", value: "Page {n} of {total}" },
             { name: "position", label: "Position", type: "select", options: POSITIONS },
             { name: "size", label: "Text size", type: "number", value: 10, min: 4, max: 72 },
             { name: "start", label: "First number", type: "number", value: 1 }, PAGES],
    run: async (v) => opened(await post("stamp", v), "Page numbers added.") },
  { id: "header", title: "Header & footer", desc: "Put the same line of text at the top or bottom of the pages.",
    fields: [{ name: "document_id", type: "doc" }, { name: "text", label: "Text", type: "text", value: "" },
             { name: "position", label: "Position", type: "select", options: [...POSITIONS].reverse() },
             { name: "size", label: "Text size", type: "number", value: 10, min: 4, max: 72 },
             { name: "color", label: "Colour", type: "color", value: "#000000" }, PAGES],
    run: async (v) => opened(await post("stamp", v), "Text added.") },
  { id: "watermark", title: "Watermark", desc: "Stamp a large see-through word across the pages.",
    fields: [{ name: "document_id", type: "doc" }, { name: "text", label: "Text", type: "text", value: "DRAFT" },
             { name: "size", label: "Text size", type: "number", value: 60, min: 8, max: 300 },
             { name: "opacity", label: "Opacity (0.05 – 1)", type: "number", value: 0.25, min: 0.05, max: 1, step: 0.05 },
             { name: "angle", label: "Angle", type: "number", value: 45, min: -90, max: 90 },
             { name: "color", label: "Colour", type: "color", value: "#cc0000" }, PAGES],
    run: async (v) => opened(await post("watermark", v), "Watermark added.") },
  { id: "crop", title: "Crop PDF", desc: "Cut margins off the pages. 72 points = 1 inch = 2.54 cm.",
    fields: [{ name: "document_id", type: "doc" },
             ...["left", "top", "right", "bottom"].map((side) => ({ name: side, label: `Cut from the ${side} (points)`, type: "number", value: 0, min: 0 })), PAGES],
    run: async (v) => opened(await post("crop", v), "Pages cropped.") },
  { id: "protect", title: "Protect PDF", desc: "Make a copy that needs a password to open (AES-256). Keep the password safe: it cannot be recovered.",
    fields: [{ name: "document_id", type: "doc" }, { name: "password", label: "Password (at least 6 characters)", type: "password" }],
    run: async (v) => ({ text: "Created a password-protected copy.", docs: [await post("protect", v)] }) },
  { id: "unlock", title: "Unlock PDF", desc: "Make a copy without the password. You must know the document's password; it is never guessed.",
    fields: [{ name: "document_id", type: "doc", encrypted: true }, { name: "password", label: "The document's password", type: "password" }],
    run: async (v) => opened(await post("unlock", v), "Created an unlocked copy.") },
  { id: "metadata", title: "Document properties", desc: "Set the title, author, subject and keywords stored in the PDF.",
    fields: [{ name: "document_id", type: "doc" }, ...["title", "author", "subject", "keywords"].map((k) => ({ name: k, label: k[0].toUpperCase() + k.slice(1), type: "text", value: "" }))],
    run: async (v) => opened(await post("metadata", v), "Properties saved.") },
];

let documents = [], current = null, picked = [];
const preselected = new URLSearchParams(location.search).get("doc");

async function loadDocuments() {
  documents = [];
  for (let offset = 0; ; offset += 200) {
    const page = await api(`/api/documents?limit=200&offset=${offset}`);
    documents.push(...page.items);
    if (documents.length >= page.total || !page.items.length) break;
  }
}

async function download(path, json) {
  const res = await api(`/api/tools/${path}`, { method: "POST", json, raw: true });
  const header = res.headers.get("Content-Disposition") || "";
  const name = decodeURIComponent((header.match(/filename\*=UTF-8''([^;]+)/) || [])[1] || "") || "download";
  const url = URL.createObjectURL(await res.blob());
  const a = el("a", { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return { text: `Downloaded “${name}”.` };
}

const docLabel = (d) => `${d.original_filename}${d.page_count ? ` (${d.page_count} page${d.page_count === 1 ? "" : "s"})` : " (password protected)"}`;

function buildField(field) {
  const id = `f-${field.name}`;
  const wrap = el("div", { className: "mb-3" });
  let input;
  if (field.type === "doc") {
    const usable = documents.filter((d) => (field.encrypted ? d.processing_status === "encrypted" || d.page_count !== null : d.page_count !== null));
    input = el("select", { id, className: "form-select", required: true }, ...usable.map((d) => el("option", { value: d.id, textContent: docLabel(d) })));
    if (usable.some((d) => d.id === preselected)) input.value = preselected;
    wrap.append(el("label", { className: "form-label", htmlFor: id, textContent: "Document" }), input);
    if (!usable.length) wrap.append(el("p", { className: "text-danger small mt-1 mb-0", textContent: "No suitable documents. Upload a PDF in My files first." }));
  } else if (field.type === "docs") {
    picked = [];
    const list = el("div", { className: "doc-pick" });
    for (const d of documents.filter((x) => x.page_count !== null)) {
      const box = el("input", { type: "checkbox", className: "form-check-input", value: d.id });
      const order = el("span", { className: "doc-order" });
      box.addEventListener("change", () => {
        picked = box.checked ? [...picked, d.id] : picked.filter((x) => x !== d.id);
        for (const row of list.children) { const i = picked.indexOf(row.dataset.id); row.querySelector(".doc-order").textContent = i < 0 ? "" : i + 1; }
      });
      const row = el("label", { className: "doc-row" }, box, order, el("span", { textContent: docLabel(d) }));
      row.dataset.id = d.id;
      list.append(row);
    }
    wrap.append(el("div", { className: "form-label", textContent: "Documents (tick at least two, in order)" }), list);
  } else if (field.type === "files") {
    input = el("input", { id, type: "file", className: "form-control", accept: field.accept, multiple: true, required: true });
    wrap.append(el("label", { className: "form-label", htmlFor: id, textContent: field.label }), input);
  } else if (field.type === "select") {
    input = el("select", { id, className: "form-select" }, ...field.options.map(([value, text]) => el("option", { value, textContent: text })));
    wrap.append(el("label", { className: "form-label", htmlFor: id, textContent: field.label }), input);
  } else {
    input = el("input", { id, type: field.type, className: field.type === "color" ? "form-control form-control-color" : "form-control", value: field.value ?? "", autocomplete: "off" });
    for (const k of ["min", "max", "step"]) if (field[k] !== undefined) input[k] = field[k];
    if (field.type === "password") input.autocomplete = "new-password";
    wrap.append(el("label", { className: "form-label", htmlFor: id, textContent: field.label }), input);
  }
  return wrap;
}

function values() {
  const out = {};
  for (const field of current.fields) {
    const input = $(`f-${field.name}`);
    if (field.type === "docs") out[field.name] = picked;
    else if (field.type === "files") out[field.name] = [...input.files];
    else if (field.type === "number") out[field.name] = Number(input.value);
    else out[field.name] = input.value;
  }
  return out;
}

function openTool(tool) {
  current = tool;
  $("tool-title").textContent = tool.title;
  $("tool-desc").textContent = tool.desc;
  $("tool-fields").replaceChildren(...tool.fields.map(buildField));
  $("tool-result").hidden = true;
  $("tool-run").textContent = tool.button || "Run";
  $("tool-run").disabled = false;
  $("tool-dialog").showModal();
}

function showResult({ text, docs = [] }) {
  const box = $("tool-result");
  box.replaceChildren(el("p", { className: "mb-1", textContent: text }),
    ...docs.map((d) => el("a", { href: `editor.html?id=${encodeURIComponent(d.id)}`, className: "d-block", textContent: `Open ${d.original_filename}` })));
  box.className = "tool-result";
  box.hidden = false;
}

$("tool-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!current) return;
  const v = values();
  if (current.fields.some((f) => f.type === "doc") && !v.document_id) return toast("Choose a document first");
  if (v.document_ids && v.document_ids.length < 2) return toast("Tick at least two documents");
  if (v.files && !v.files.length) return toast("Choose at least one picture");
  $("tool-run").disabled = true;
  $("tool-run").textContent = "Working…";
  try {
    showResult(await current.run(v));
    await loadDocuments();
  } catch (err) {
    const box = $("tool-result");
    box.replaceChildren(el("p", { className: "mb-0", textContent: err.message || "The tool failed" }));
    box.className = "tool-result failed";
    box.hidden = false;
  } finally {
    $("tool-run").disabled = false;
    $("tool-run").textContent = current.button || "Run";
  }
});
$("tool-close").addEventListener("click", () => $("tool-dialog").close());
$("logout").addEventListener("click", async () => { await logout(); location.replace("index.html"); });

(async () => {
  const user = await requireUser();
  $("user-name").textContent = user.full_name || user.email;
  await loadDocuments();
  $("tool-grid").replaceChildren(...TOOLS.map((tool) => {
    const card = el("button", { type: "button", className: "tool-card" }, el("strong", { textContent: tool.title }), el("span", { textContent: tool.desc }));
    card.dataset.tool = tool.id;
    card.addEventListener("click", () => openTool(tool));
    return card;
  }));
  const wanted = new URLSearchParams(location.search).get("tool");
  if (wanted && TOOLS.some((t) => t.id === wanted)) openTool(TOOLS.find((t) => t.id === wanted));
})().catch((e) => { console.error(e); toast(e.message || "The tools could not be loaded"); });
