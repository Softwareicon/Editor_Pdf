// PDF viewer and page organiser. Rendering is done by PDF.js; page geometry comes from the backend so the
// whole document can be laid out immediately and pages are only rendered when they scroll into view.
// Every page edit is performed by the server on the real file and stored as a new version (undo/redo).
import * as pdfjsLib from "../vendor/pdfjs/pdf.min.mjs";
import { api, formatBytes, requireUser, toast, uploadPdf } from "./api.js";
import { createEditTools } from "./edit-tools.js";

const VENDOR = new URL("../vendor/pdfjs/", import.meta.url).href;
pdfjsLib.GlobalWorkerOptions.workerSrc = `${VENDOR}pdf.worker.min.mjs`;

const CSS_UNITS = 96 / 72; // PDF points -> CSS pixels at 100%
const MIN_SCALE = 0.1, MAX_SCALE = 5, MAX_CANVAS_PIXELS = 16_777_216, GAP = 16;
const $ = (id) => document.getElementById(id);
const viewer = $("viewer"), pagesEl = $("pages");

const state = {
  docId: new URLSearchParams(location.search).get("id"),
  doc: null, pdf: null, blob: null, scale: 1, fitMode: "width", current: 1,
  pages: [],            // [{ div, canvas, layer, w, h, renderTask, renderedScale }], w/h in PDF points as displayed
  visible: new Set(), selected: new Set(), busy: false, text: new Map(), matches: [], matchIndex: -1, query: "", searchRun: 0,
};

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}
const setStatus = (text) => { $("status").textContent = text; };

// Text, whiteout, shapes, pen, images and signatures drawn on top of the pages (see edit-tools.js).
const edits = createEditTools({
  state, pagesEl, viewer, CSS_UNITS,
  onChange: () => { updateTools(); if (edits.hasPending() && !state.busy) setStatus("Unsaved edits — click Save edits to write them into the PDF"); },
  save: () => saveEdits(),
  textRuns: (n) => textRuns(n),
});

// Text runs of a page for the "Edit text" and "Replace" tools, in display points (origin top-left, y down).
async function textRuns(n) {
  const page = await state.pdf.getPage(n);
  const vp = page.getViewport({ scale: 1 }), v = vp.transform;
  const content = await page.getTextContent();
  return content.items.filter((item) => item.str && item.str.trim()).map((item) => {
    const m = item.transform;
    const t = [v[0] * m[0] + v[2] * m[1], v[1] * m[0] + v[3] * m[1], v[0] * m[2] + v[2] * m[3], v[1] * m[2] + v[3] * m[3],
               v[0] * m[4] + v[2] * m[5] + v[4], v[1] * m[4] + v[3] * m[5] + v[5]];
    const r = itemRect(vp, item);
    let name = content.styles[item.fontName]?.fontFamily || "";
    try { name = `${page.commonObjs.get(item.fontName)?.name || ""} ${name}`; } catch { /* font not loaded yet */ }
    return {
      str: item.str, x: r.left, y: r.top, w: r.width, h: r.height, baseline: t[5], size: Math.hypot(t[2], t[3]) || item.height,
      upright: t[0] > 0 && t[3] < 0 && Math.abs(t[1]) < 0.02 * Math.abs(t[0]) && Math.abs(t[2]) < 0.02 * Math.abs(t[3]),
      font: /courier|mono|consol/i.test(name) ? "courier" : /times|georgia|garamond|cambria|(^|[^-])\bserif/i.test(name) && !/sans/i.test(name) ? "times" : "helvetica",
      bold: /bold|black|heavy/i.test(name), italic: /italic|oblique/i.test(name),
    };
  });
}
function showMessage(text) { $("message").textContent = text; $("message").hidden = false; pagesEl.hidden = true; }

// ---------- loading ----------
function askPassword(reason) {
  return new Promise((resolve) => {
    const dialog = $("password-dialog"), input = $("pdf-password");
    $("password-hint").classList.toggle("text-danger", reason === pdfjsLib.PasswordResponses.INCORRECT_PASSWORD);
    if (reason === pdfjsLib.PasswordResponses.INCORRECT_PASSWORD) $("password-hint").textContent = "Incorrect password. Please try again.";
    input.value = "";
    dialog.returnValue = "cancel";
    dialog.onclose = () => resolve(dialog.returnValue === "ok" ? input.value : null);
    dialog.showModal();
    input.focus();
  });
}

async function load() {
  await requireUser();
  if (!state.docId) return showMessage("No document selected. Go back to your files and open one.");
  await openDocument(1);
}

// (Re)load the document's current version. Called on start and after every edit, undo and redo.
async function openDocument(page = state.current) {
  try {
    state.doc = await api(`/api/documents/${encodeURIComponent(state.docId)}`);
  } catch (e) {
    return showMessage(e.status === 404 ? "This document does not exist or you do not have access to it." : e.message);
  }
  $("doc-name").textContent = state.doc.original_filename;
  document.title = `${state.doc.original_filename} · PDF Master Pro`;

  setStatus(`Loading ${formatBytes(state.doc.file_size)}…`);
  const [res, geometry] = await Promise.all([
    api(`/api/documents/${state.docId}/download`, { raw: true }),
    api(`/api/editor/${state.docId}/pages`).catch(() => null),
  ]);
  const blob = await res.blob();
  const task = pdfjsLib.getDocument({
    data: await blob.arrayBuffer(), cMapUrl: `${VENDOR}cmaps/`, cMapPacked: true,
    standardFontDataUrl: `${VENDOR}standard_fonts/`, isEvalSupported: false, enableXfa: false,
  });
  task.onPassword = async (update, reason) => {
    const password = await askPassword(reason);
    if (password === null) task.destroy(); else update(password);
  };
  let pdf;
  try {
    pdf = await task.promise;
  } catch (e) {
    if (e?.name === "PasswordException" || task.destroyed) return showMessage("A password is required to view this document.");
    console.error(e);
    return showMessage("This PDF could not be displayed. The file may be damaged or use unsupported features.");
  }

  // Swap in the new document only once it has opened, so a failed reload leaves the old view intact.
  teardown();
  state.blob = blob;
  state.pdf = pdf;
  $("download").disabled = $("print").disabled = false;
  const count = pdf.numPages;
  let sizes = geometry?.pages?.length === count ? geometry.pages.map(displaySize) : null;
  if (!sizes) { // e.g. password-protected file the server cannot read: fall back to page 1, corrected as pages render
    const vp = (await pdf.getPage(1)).getViewport({ scale: 1 });
    sizes = Array.from({ length: count }, () => ({ w: vp.width, h: vp.height }));
  }
  state.current = Math.min(count, Math.max(1, page));
  buildPages(sizes);
  buildThumbnails();
  $("page-total").textContent = count;
  Object.assign($("page-input"), { max: count, disabled: false });
  if (state.fitMode) applyFit(state.fitMode); else updateZoomLabel();
  goToPage(state.current);
  updateTools();
  setStatus(state.doc.current_version > 1 ? "All changes saved" : "Ready");
  loadOutline().catch(console.error);
  loadAttachments().catch(console.error);
  if (state.query) runSearch(state.query, false);
}

function teardown() {
  pageObserver.disconnect();
  thumbObserver.disconnect();
  for (const p of state.pages) p.renderTask?.task.cancel();
  state.pdf?.destroy();
  state.pages = []; state.thumbs = [];
  state.visible.clear(); state.selected.clear(); state.text.clear();
  state.matches = []; state.matchIndex = -1; state.searchRun++;
}

function displaySize(p) {
  return p.rotation % 180 === 0 ? { w: p.width, h: p.height } : { w: p.height, h: p.width };
}

// ---------- page layout & lazy rendering ----------
const pageObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    const n = Number(entry.target.dataset.page);
    if (entry.isIntersecting) { state.visible.add(n); renderPage(n); }
    else { state.visible.delete(n); releasePage(n); }
  }
}, { root: viewer, rootMargin: "800px 0px" });

function buildPages(sizes) {
  pagesEl.replaceChildren();
  state.pages = sizes.map((size, i) => {
    const canvas = el("canvas");
    const layer = el("div", { className: "hl-layer" });
    const div = el("div", { className: "page" }, canvas, layer);
    div.dataset.page = i + 1;
    div.setAttribute("aria-label", `Page ${i + 1}`);
    pagesEl.append(div);
    return { div, canvas, layer, ...size, renderTask: null, renderedScale: 0 };
  });
  sizePages();
  edits.attach();
  for (const p of state.pages) pageObserver.observe(p.div);
}

function sizePages() {
  for (const p of state.pages) {
    p.div.style.width = `${Math.floor(p.w * state.scale * CSS_UNITS)}px`;
    p.div.style.height = `${Math.floor(p.h * state.scale * CSS_UNITS)}px`;
  }
}

async function renderPage(n) {
  const p = state.pages[n - 1], scale = state.scale;
  if (!p || p.renderedScale === scale || p.renderTask?.scale === scale) return;
  p.renderTask?.task.cancel();
  const page = await state.pdf.getPage(n);
  if (!state.visible.has(n) || state.scale !== scale) return;
  const vp = page.getViewport({ scale: scale * CSS_UNITS });
  if (Math.abs(vp.width / (scale * CSS_UNITS) - p.w) > 0.5 || Math.abs(vp.height / (scale * CSS_UNITS) - p.h) > 0.5) {
    p.w = vp.width / (scale * CSS_UNITS); p.h = vp.height / (scale * CSS_UNITS); // trust the renderer's geometry
    p.div.style.width = `${Math.floor(vp.width)}px`; p.div.style.height = `${Math.floor(vp.height)}px`;
    edits.syncPage(n);
  }
  let ratio = window.devicePixelRatio || 1;
  const pixels = vp.width * vp.height * ratio * ratio;
  if (pixels > MAX_CANVAS_PIXELS) ratio *= Math.sqrt(MAX_CANVAS_PIXELS / pixels); // stay within browser canvas limits
  // Render off-screen and swap, so the previous image stays visible while zooming.
  const buffer = el("canvas", { width: Math.floor(vp.width * ratio), height: Math.floor(vp.height * ratio) });
  const task = page.render({ canvasContext: buffer.getContext("2d", { alpha: false }), viewport: vp,
                             transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : undefined });
  p.renderTask = { task, scale };
  try {
    await task.promise;
  } catch (e) {
    if (e?.name !== "RenderingCancelledException") { console.error(e); setStatus(`Page ${n} could not be rendered`); }
    return;
  } finally {
    if (p.renderTask?.task === task) p.renderTask = null;
  }
  p.canvas.replaceWith(buffer);
  p.canvas.width = p.canvas.height = 0;
  p.canvas = buffer;
  p.renderedScale = scale;
  p.viewport = vp;
  drawHighlights(n);
}

function releasePage(n) {
  const p = state.pages[n - 1];
  p.renderTask?.task.cancel();
  p.renderTask = null;
  p.canvas.width = p.canvas.height = 0; // free bitmap memory for off-screen pages
  p.renderedScale = 0;
  p.layer.replaceChildren();
}

function setScale(scale, mode = null) {
  scale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
  state.fitMode = mode;
  if (scale === state.scale) return updateZoomLabel();
  const anchor = state.pages[state.current - 1];
  const offsetRatio = anchor ? (viewer.scrollTop - anchor.div.offsetTop) / anchor.div.offsetHeight : 0;
  state.scale = scale;
  sizePages();
  edits.refresh();
  if (anchor) viewer.scrollTop = anchor.div.offsetTop + offsetRatio * anchor.div.offsetHeight;
  for (const n of state.visible) renderPage(n);
  updateZoomLabel();
}

function applyFit(mode) {
  const p = state.pages[state.current - 1];
  if (!p) return;
  const availW = viewer.clientWidth - 2 * GAP - 2, availH = viewer.clientHeight - 2 * GAP - 2;
  const byWidth = availW / (p.w * CSS_UNITS);
  setScale(mode === "page" ? Math.min(byWidth, availH / (p.h * CSS_UNITS)) : byWidth, mode);
  if (mode === "page") goToPage(state.current);
}

function updateZoomLabel() {
  const text = `${Math.round(state.scale * 100)}%`;
  $("zoom-label").textContent = text;
  $("status-zoom").textContent = `Zoom ${text}${state.fitMode ? ` (fit ${state.fitMode})` : ""}`;
  $("zoom-out").disabled = state.scale <= MIN_SCALE;
  $("zoom-in").disabled = state.scale >= MAX_SCALE;
}

const ZOOM_STEPS = [0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4, 5];
const zoomIn = () => setScale(ZOOM_STEPS.find((s) => s > state.scale + 0.001) ?? MAX_SCALE);
const zoomOut = () => setScale([...ZOOM_STEPS].reverse().find((s) => s < state.scale - 0.001) ?? MIN_SCALE);

// ---------- navigation ----------
function goToPage(n, smooth = false) {
  n = Math.min(state.pages.length, Math.max(1, Math.round(n) || 1));
  const p = state.pages[n - 1];
  if (p) viewer.scrollTo({ top: p.div.offsetTop - GAP / 2, behavior: smooth ? "smooth" : "auto" });
  setCurrent(n);
}

function setCurrent(n) {
  // Same page: resync the box only if the user is not typing a page number into it right now.
  if (n === state.current && ($("page-input").value === String(n) || document.activeElement === $("page-input"))) return;
  state.thumbs?.[state.current - 1]?.classList.remove("active");
  state.current = n;
  $("page-input").value = n;
  const thumb = state.thumbs?.[n - 1];
  if (thumb) { thumb.classList.add("active"); if (!$("thumbs").hidden) thumb.scrollIntoView({ block: "nearest" }); }
}

let scrollQueued = false;
viewer.addEventListener("scroll", () => {
  if (scrollQueued) return;
  scrollQueued = true;
  requestAnimationFrame(() => {
    scrollQueued = false;
    if (!state.pages.length) return;
    const probe = viewer.scrollTop + viewer.clientHeight / 3;
    let lo = 0, hi = state.pages.length - 1; // last page whose top is above the probe line
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (state.pages[mid].div.offsetTop <= probe) lo = mid; else hi = mid - 1;
    }
    setCurrent(lo + 1);
  });
}, { passive: true });

// ---------- thumbnails ----------
const THUMB_WIDTH = 132;
const thumbObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) if (entry.isIntersecting) { thumbObserver.unobserve(entry.target); renderThumb(Number(entry.target.dataset.page)); }
}, { root: $("thumbs"), rootMargin: "300px 0px" });

function buildThumbnails() {
  state.thumbs = state.pages.map((p, i) => {
    const frame = el("div", { className: "thumb-frame" });
    frame.style.aspectRatio = `${p.w} / ${p.h}`;
    const button = el("button", { className: "thumb", type: "button", title: `Page ${i + 1}` }, frame, el("span", { textContent: i + 1 }));
    button.dataset.page = i + 1;
    button.addEventListener("click", (e) => selectThumb(i + 1, e));
    thumbObserver.observe(button);
    return button;
  });
  $("thumbs").replaceChildren(...state.thumbs);
  state.thumbs[state.current - 1]?.classList.add("active");
}

// ---------- page selection & editing ----------
function selectThumb(n, event) {
  if (event.ctrlKey || event.metaKey) {
    if (state.selected.has(n)) state.selected.delete(n); else state.selected.add(n);
  } else if (event.shiftKey) {
    const from = Math.min(n, state.current), to = Math.max(n, state.current);
    for (let i = from; i <= to; i++) state.selected.add(i);
  } else {
    state.selected.clear();
    state.selected.add(n);
    goToPage(n);
  }
  updateTools();
}

// Pages an action applies to: the selected thumbnails, or the current page when nothing is selected.
const targets = () => (state.selected.size ? [...state.selected].sort((a, b) => a - b) : [state.current]);

function updateTools() {
  const editable = Boolean(state.pdf) && state.doc?.processing_status !== "encrypted" && !state.busy;
  state.thumbs?.forEach((t, i) => t.classList.toggle("selected", state.selected.has(i + 1)));
  for (const b of document.querySelectorAll("#page-tools button, #doc-tools button")) b.disabled = !editable;
  if (editable && targets().length >= state.pages.length) $("delete-pages").disabled = true; // a PDF needs one page
  if (editable && state.pages.length < 2) $("reverse").disabled = true;
  $("undo").disabled = state.busy || !(state.doc?.can_undo || edits.hasPending());
  $("redo").disabled = state.busy || !state.doc?.can_redo || edits.hasPending();
  edits.update(editable);
  sortable?.option("disabled", !editable);
  const t = targets();
  $("selection").textContent = !state.pdf ? "" : state.doc.processing_status === "encrypted"
    ? "Password-protected PDFs cannot be edited yet"
    : state.selected.size > 1 ? `${t.length} pages selected` : `Page ${t[0]} selected · Ctrl/Shift-click to select more · drag thumbnails to reorder`;
}

// Run one server-side edit, then reload the new version in place.
async function edit(label, request, page = state.current, isSave = false) {
  if (state.busy) return null;
  if (!isSave && edits.hasPending() && !(await saveEdits())) return null; // page numbers are about to change: save first
  state.busy = true;
  updateTools();
  setStatus(`${label}…`);
  try {
    const result = await request();
    state.busy = false;
    await openDocument(page);
    return result;
  } catch (e) {
    state.busy = false;
    toast(e.message);
    await openDocument(page).catch(console.error); // also restores thumbnail order after a failed drag
    return null;
  }
}

// Write the unsaved overlay objects into the PDF (a new version, so Undo takes the whole save back).
async function saveEdits() {
  edits.finishTyping();
  if (!edits.hasPending()) return true;
  let items;
  try { items = edits.buildPayload(); } catch (e) { toast(e.message); return false; }
  const count = items.length;
  const done = await edit("Saving edits", async () => {
    const doc = await api("/api/edit/apply", { method: "POST", json: { document_id: state.docId, items } });
    edits.clear();
    return doc;
  }, state.current, true);
  if (done) toast(`Saved ${count} edit${count === 1 ? "" : "s"} into the PDF. Use Undo to take ${count === 1 ? "it" : "them"} back.`, "ok");
  return Boolean(done);
}

const pageOp = (op, body) => api(`/api/pages/${op}`, { method: "POST", json: { document_id: state.docId, ...body } });

const sortable = window.Sortable ? new window.Sortable($("thumbs"), {
  animation: 120, forceFallback: true, fallbackTolerance: 4, ghostClass: "drag-ghost",
  onEnd: (e) => {
    if (e.oldIndex === e.newIndex) return;
    const order = [...$("thumbs").children].map((t) => Number(t.dataset.page));
    edit("Reordering pages", () => pageOp("reorder", { order }), e.newIndex + 1);
  },
}) : null;

$("rotate-left").addEventListener("click", () => edit("Rotating", () => pageOp("rotate", { pages: targets(), angle: 270 })));
$("rotate-right").addEventListener("click", () => edit("Rotating", () => pageOp("rotate", { pages: targets(), angle: 90 })));
$("duplicate").addEventListener("click", () => edit("Duplicating", () => pageOp("duplicate", { pages: targets() })));
$("reverse").addEventListener("click", () => edit("Reversing page order", () => pageOp("reverse", {}), 1));
$("add-blank").addEventListener("click", () =>
  edit("Adding a blank page", () => pageOp("add", { after: state.current }), state.current + 1));
$("delete-pages").addEventListener("click", async () => {
  const pages = targets();
  const done = await edit("Deleting", () => pageOp("delete", { pages }), Math.max(1, pages[0] - 1));
  if (done) toast(`Deleted ${pages.length} page${pages.length === 1 ? "" : "s"}. Use Undo to bring ${pages.length === 1 ? "it" : "them"} back.`, "ok");
});
$("extract").addEventListener("click", async () => {
  const doc = await edit("Extracting", () => pageOp("extract", { pages: targets() }));
  if (!doc) return;
  const note = toast(`Created "${doc.original_filename}". `, "ok");
  note.append(el("a", { href: `editor.html?id=${encodeURIComponent(doc.id)}`, textContent: "Open it", className: "text-white" }));
});
$("undo").addEventListener("click", () => {
  if (edits.hasPending()) return edits.undoLocal(); // unsaved objects are taken back one at a time first
  edit("Undoing", () => api(`/api/editor/${state.docId}/undo`, { method: "POST" }));
});
$("redo").addEventListener("click", () => edit("Redoing", () => api(`/api/editor/${state.docId}/redo`, { method: "POST" })));

async function renderThumb(n) {
  try {
    const page = await state.pdf.getPage(n);
    const base = page.getViewport({ scale: 1 });
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const vp = page.getViewport({ scale: (THUMB_WIDTH / base.width) * ratio });
    const canvas = el("canvas", { width: Math.floor(vp.width), height: Math.floor(vp.height) });
    await page.render({ canvasContext: canvas.getContext("2d", { alpha: false }), viewport: vp }).promise;
    const frame = state.thumbs[n - 1].firstChild;
    frame.style.aspectRatio = `${base.width} / ${base.height}`;
    frame.replaceChildren(canvas);
  } catch (e) { console.error(e); }
}

// ---------- outline (bookmarks) & attachments ----------
async function destinationPage(dest) {
  if (typeof dest === "string") dest = await state.pdf.getDestination(dest);
  if (!Array.isArray(dest) || dest[0] == null) return null;
  return typeof dest[0] === "object" ? (await state.pdf.getPageIndex(dest[0])) + 1 : dest[0] + 1;
}

async function loadOutline() {
  const outline = await state.pdf.getOutline();
  if (!outline?.length) return $("outline").replaceChildren(el("p", { className: "side-empty", textContent: "This document has no outline." }));
  const build = (items) => el("ul", {}, ...items.map((item) => {
    const link = el("button", { type: "button", textContent: item.title || "(untitled)", disabled: !item.dest });
    link.addEventListener("click", async () => {
      try { const n = await destinationPage(item.dest); if (n) goToPage(n); }
      catch { toast("This bookmark points to a missing page"); }
    });
    return el("li", {}, link, ...(item.items?.length ? [build(item.items)] : []));
  }));
  $("outline").replaceChildren(build(outline));
}

async function loadAttachments() {
  const files = Object.values((await state.pdf.getAttachments()) || {});
  if (!files.length) return $("attachments").replaceChildren(el("p", { className: "side-empty", textContent: "This document has no attachments." }));
  $("attachments").replaceChildren(el("ul", {}, ...files.map((file) => {
    const name = (file.filename || "attachment").replace(/[\\/\x00-\x1f]/g, "_");
    const button = el("button", { type: "button", title: `Save ${name}`, textContent: `${name} (${formatBytes(file.content.length)})` });
    // Always saved as a download with a generic type; attachments are never opened or executed in the page.
    button.addEventListener("click", () => saveBlob(new Blob([file.content], { type: "application/octet-stream" }), name));
    return el("li", {}, button);
  })));
}

for (const tab of document.querySelectorAll(".side-tabs button")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".side-tabs button")) {
      other.classList.toggle("active", other === tab);
      $(other.dataset.panel).hidden = other !== tab;
    }
    if (tab.dataset.panel === "thumbs") state.thumbs?.[state.current - 1]?.scrollIntoView({ block: "nearest" });
  });
}

// ---------- search ----------
async function pageText(n) {
  if (!state.text.has(n)) {
    const content = await (await state.pdf.getPage(n)).getTextContent();
    let text = "";
    const spans = []; // [start, end, item] offsets of each text run in the page string
    for (const item of content.items) {
      if (item.str) { spans.push([text.length, text.length + item.str.length, item]); text += item.str; }
      if (item.hasEOL) text += " ";
    }
    state.text.set(n, { text: text.toLowerCase(), spans });
  }
  return state.text.get(n);
}

async function runSearch(query, jump = true) {
  const run = ++state.searchRun;
  query = query.trim().toLowerCase();
  state.query = query; state.matches = []; state.matchIndex = -1;
  for (const n of state.visible) drawHighlights(n);
  updateSearchUi();
  if (!query || !state.pdf) return;
  for (let n = 1; n <= state.pdf.numPages; n++) {
    const { text, spans } = await pageText(n);
    if (run !== state.searchRun) return; // superseded by a newer search
    for (let at = text.indexOf(query); at !== -1; at = text.indexOf(query, at + query.length)) {
      const items = spans.filter(([start, end]) => start < at + query.length && end > at).map((s) => s[2]);
      state.matches.push({ page: n, items });
    }
    if (n % 20 === 0 || n === state.pdf.numPages) { setStatus(`Searching… page ${n} of ${state.pdf.numPages}`); updateSearchUi(); }
  }
  setStatus(state.matches.length ? "Ready" : "No matches found");
  if (state.matches.length && jump) stepMatch(1, state.matches.findIndex((m) => m.page >= state.current));
  else { if (state.matches.length) state.matchIndex = 0; for (const n of state.visible) drawHighlights(n); updateSearchUi(); }
}

function stepMatch(direction, startAt = null) {
  const total = state.matches.length;
  if (!total) return;
  state.matchIndex = startAt !== null ? (startAt === -1 ? 0 : startAt) : (state.matchIndex + direction + total) % total;
  const match = state.matches[state.matchIndex], p = state.pages[match.page - 1];
  let top = p.div.offsetTop - GAP / 2;
  if (p.viewport && match.items[0]) top = p.div.offsetTop + itemRect(p.viewport, match.items[0]).top - viewer.clientHeight / 3;
  viewer.scrollTo({ top: Math.max(0, top) });
  setCurrent(match.page);
  for (const n of state.visible) drawHighlights(n);
  updateSearchUi();
}

function updateSearchUi() {
  const total = state.matches.length;
  $("search-count").textContent = !state.query ? "" : total ? `${state.matchIndex + 1} of ${total}` : "0 results";
  $("search-prev").disabled = $("search-next").disabled = total === 0;
}

function itemRect(viewport, item) {
  const [, , , , x, y] = item.transform;
  const height = item.height || Math.hypot(item.transform[2], item.transform[3]);
  const [x1, y1, x2, y2] = viewport.convertToViewportRectangle([x, y - height * 0.22, x + item.width, y + height * 0.85]);
  return { left: Math.min(x1, x2), top: Math.min(y1, y2), width: Math.abs(x2 - x1), height: Math.abs(y2 - y1) };
}

// Highlights mark the whole text run(s) containing a match (run boundaries are defined by the PDF itself).
function drawHighlights(n) {
  const p = state.pages[n - 1];
  if (!p) return;
  p.layer.replaceChildren();
  if (!p.viewport || p.renderedScale !== state.scale) return;
  state.matches.forEach((match, index) => {
    if (match.page !== n) return;
    for (const item of match.items) {
      const r = itemRect(p.viewport, item);
      const mark = el("div", { className: index === state.matchIndex ? "hl current" : "hl" });
      Object.assign(mark.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
      p.layer.append(mark);
    }
  });
}

// ---------- download / print / open ----------
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function printDocument() {
  document.getElementById("print-frame")?.remove();
  const url = URL.createObjectURL(new Blob([state.blob], { type: "application/pdf" }));
  const frame = el("iframe", { id: "print-frame", src: url, title: "Print" });
  frame.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0";
  frame.onload = () => {
    try { frame.contentWindow.focus(); frame.contentWindow.print(); }
    catch { window.open(url, "_blank", "noopener"); } // browsers without an embeddable PDF viewer: print from the new tab
  };
  document.body.append(frame);
}

$("download").addEventListener("click", () => saveBlob(state.blob, state.doc.original_filename));
$("print").addEventListener("click", printDocument);
$("open").addEventListener("click", () => $("open-input").click());
$("open-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  try {
    const doc = await uploadPdf(file, (p) => setStatus(p < 1 ? `Uploading ${Math.round(p * 100)}%…` : "Checking file…"));
    location.href = `editor.html?id=${encodeURIComponent(doc.id)}`;
  } catch (err) { setStatus("Upload failed"); toast(err.message); }
});

// ---------- toolbar wiring ----------
$("zoom-in").addEventListener("click", zoomIn);
$("zoom-out").addEventListener("click", zoomOut);
$("fit-page").addEventListener("click", () => applyFit("page"));
$("fit-width").addEventListener("click", () => applyFit("width"));
$("page-input").addEventListener("change", (e) => goToPage(Number(e.target.value)));
$("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const query = $("search").value.trim().toLowerCase();
  if (query && query === state.query && state.matches.length) stepMatch(1); else runSearch(query);
});
$("search").addEventListener("input", (e) => { if (!e.target.value) runSearch(""); });
$("search-next").addEventListener("click", () => stepMatch(1));
$("search-prev").addEventListener("click", () => stepMatch(-1));

document.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "f") { e.preventDefault(); $("search").focus(); $("search").select(); }
  else if (mod && (e.key === "+" || e.key === "=")) { e.preventDefault(); zoomIn(); }
  else if (mod && e.key === "-") { e.preventDefault(); zoomOut(); }
  else if (mod && e.key.toLowerCase() === "p" && state.blob) { e.preventDefault(); printDocument(); }
  else if (mod && !/^(INPUT|TEXTAREA)$/.test(e.target.tagName) && e.key.toLowerCase() === "z") {
    e.preventDefault();
    $(e.shiftKey ? "redo" : "undo").click();
  } else if (mod && !/^(INPUT|TEXTAREA)$/.test(e.target.tagName) && e.key.toLowerCase() === "y") { e.preventDefault(); $("redo").click(); }
});
viewer.addEventListener("wheel", (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  if (e.deltaY < 0) zoomIn(); else zoomOut();
}, { passive: false });

window.addEventListener("beforeunload", (e) => { if (edits.hasPending()) e.preventDefault(); });

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (state.fitMode) applyFit(state.fitMode); }, 150);
});

load().catch((e) => { console.error(e); showMessage(e.message || "The document could not be loaded."); setStatus("Error"); });
