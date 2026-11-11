// Content editing by overlay: text, whiteout, highlight, shapes, lines, freehand pen, images and signatures.
// Objects live in an SVG layer on top of each page (coordinates in PDF points, so they follow the zoom) until
// "Save edits" sends them to the server, which draws them into the real PDF as a new version.
import { toast } from "./api.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const LINE_HEIGHT = 1.2, ASCENT = 0.9; // must match backend/app/workers/overlay_ops.py
const FAMILIES = {
  helvetica: 'Arial, Helvetica, "Liberation Sans", sans-serif',
  times: '"Times New Roman", Times, "Liberation Serif", serif',
  courier: '"Courier New", Courier, "Liberation Mono", monospace',
};
// Characters the built-in PDF fonts can show. Any other text (Hindi, emoji, …) is saved as a picture of the text.
const CP1252 = /^[\n\x20-\x7E\xA0-\xFF€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ]*$/;
const STICKY_TOOLS = new Set(["pen", "highlight", "whiteout", "strike", "underline", "tick", "cross"]);
const SIGNATURE_FONTS = [
  'italic 96px "Brush Script MT", "Segoe Script", "Lucida Handwriting", "Apple Chancery", cursive',
  '96px "Segoe Script", "Lucida Handwriting", "Bradley Hand", cursive',
  'italic 96px Georgia, "Times New Roman", serif',
];
const hex = (r, g, b) => "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join(""); // stay active after each stroke
const MAX_IMAGE_SIDE = 2000;

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const round = (v) => Math.round(v * 100) / 100;

function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, v);
  return node;
}

function arrowHead(item) {
  const length = Math.hypot(item.x2 - item.x1, item.y2 - item.y1);
  if (!item.arrow || !length) return null;
  const head = Math.min(Math.max(4 * item.width, 8), length);
  const ux = (item.x2 - item.x1) / length, uy = (item.y2 - item.y1) / length;
  const bx = item.x2 - ux * head, by = item.y2 - uy * head;
  return { bx, by, points: `${item.x2},${item.y2} ${bx - uy * head * 0.45},${by + ux * head * 0.45} ${bx + uy * head * 0.45},${by - ux * head * 0.45}` };
}

function loadImage(src) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("This image could not be read"));
    img.src = src;
  });
}

export function createEditTools(ctx) {
  const { state, pagesEl, viewer, CSS_UNITS } = ctx;
  let items = [];           // pending (unsaved) objects, in drawing order
  let selectedId = null, tool = "select", drag = null, editor = null, seq = 0, enabled = false;

  const find = (id) => items.find((i) => i.id === id);
  const pxPerPt = () => state.scale * CSS_UNITS;
  const prop = {
    color: () => $("edit-color").value,
    size: () => Number($("edit-size").value) || 14,
    width: () => Number($("edit-width").value) || 1.5,
    font: () => $("edit-font").value,
    bold: () => $("edit-bold").classList.contains("active"),
    italic: () => $("edit-italic").classList.contains("active"),
    fill: () => ($("edit-fill-on").classList.contains("active") ? $("edit-fill").value : null),
  };

  // ---------- drawing the layer ----------
  function drawItem(item) {
    const g = svg("g", { "data-id": item.id, class: "edit-item" });
    const hit = (node) => { // generous invisible outline so thin objects are easy to grab
      const clone = node.cloneNode(false);
      for (const [k, v] of [["stroke", "transparent"], ["stroke-width", Math.max(10 / pxPerPt(), Number(node.getAttribute("stroke-width")) || 0)],
                            ["fill", "none"], ["pointer-events", "stroke"], ["opacity", null]]) v === null ? clone.removeAttribute(k) : clone.setAttribute(k, v);
      return clone;
    };
    if (item.type === "text") {
      const text = svg("text", { "font-size": item.size, fill: item.color, "font-family": FAMILIES[item.font],
                                 "font-weight": item.bold ? "bold" : "normal", "font-style": item.italic ? "italic" : "normal" });
      text.style.whiteSpace = "pre";
      item.text.split("\n").forEach((line, i) => {
        const span = svg("tspan", { x: item.x, y: item.y + ASCENT * item.size + i * LINE_HEIGHT * item.size });
        span.textContent = line || " ";
        text.append(span);
      });
      g.append(text);
    } else if (item.type === "rect" || item.type === "ellipse") {
      const common = { fill: item.fill || "none", stroke: item.stroke || "none", "stroke-width": item.stroke ? item.stroke_width : null, opacity: item.opacity };
      const shape = item.type === "rect"
        ? svg("rect", { x: item.x, y: item.y, width: item.w, height: item.h, ...common })
        : svg("ellipse", { cx: item.x + item.w / 2, cy: item.y + item.h / 2, rx: item.w / 2, ry: item.h / 2, ...common });
      if (item.kind === "whiteout") shape.classList.add("whiteout");
      g.append(shape);
      if (!item.fill) g.append(hit(shape));
    } else if (item.type === "line") {
      const head = arrowHead(item);
      const line = svg("line", { x1: item.x1, y1: item.y1, x2: head ? head.bx : item.x2, y2: head ? head.by : item.y2,
                                 stroke: item.color, "stroke-width": item.width, "stroke-linecap": "round" });
      g.append(line);
      if (head) g.append(svg("polygon", { points: head.points, fill: item.color }));
      g.append(hit(svg("line", { x1: item.x1, y1: item.y1, x2: item.x2, y2: item.y2, "stroke-width": item.width })));
    } else if (item.type === "ink") {
      const line = svg("polyline", { points: item.points.map((p) => p.join(",")).join(" "), fill: "none", stroke: item.color,
                                     "stroke-width": item.width, "stroke-linecap": "round", "stroke-linejoin": "round" });
      g.append(line, hit(line));
    } else if (item.type === "link") {
      const area = svg("rect", { class: "edit-link", x: item.x, y: item.y, width: item.w, height: item.h, "stroke-width": 1 / pxPerPt(),
                                 "stroke-dasharray": `${4 / pxPerPt()} ${3 / pxPerPt()}` });
      const tip = svg("title");
      tip.textContent = item.url || "Link";
      area.append(tip);
      g.append(area);
    } else if (item.type === "field") {
      g.append(svg("rect", { class: "edit-field", x: item.x, y: item.y, width: item.w, height: item.h, "stroke-width": 1 / pxPerPt() }));
      const label = svg("text", { class: "edit-field-label", x: item.x + 2, y: item.y + Math.min(item.h * 0.75, 9), "font-size": Math.min(8, item.h * 0.7) });
      label.textContent = item.field === "checkbox" ? "" : item.name;
      g.append(label);
    } else if (item.type === "image") {
      g.append(svg("image", { href: item.data, x: item.x, y: item.y, width: item.w, height: item.h, preserveAspectRatio: "none" }));
    }
    return g;
  }

  function renderPage(n) {
    const layer = state.pages[n - 1]?.editLayer;
    if (!layer) return;
    layer.replaceChildren(...items.filter((i) => i.page === n && i !== editor?.item).map(drawItem));
    const item = find(selectedId);
    if (!item || item.page !== n || item === editor?.item) return;
    const node = layer.querySelector(`[data-id="${item.id}"]`);
    let box;
    try { box = node.getBBox(); } catch { return; }
    const pad = 3 / pxPerPt(), hs = 10 / pxPerPt();
    layer.append(svg("rect", { class: "edit-selection", x: box.x - pad, y: box.y - pad, width: box.width + 2 * pad, height: box.height + 2 * pad,
                               "stroke-width": 1.2 / pxPerPt(), "stroke-dasharray": `${4 / pxPerPt()} ${3 / pxPerPt()}` }));
    if (item.type !== "ink") {
      const at = item.type === "line" ? { x: item.x2, y: item.y2 } : { x: box.x + box.width + pad, y: box.y + box.height + pad };
      layer.append(svg("rect", { class: "edit-handle", "data-handle": "1", x: at.x - hs / 2, y: at.y - hs / 2, width: hs, height: hs,
                                 "stroke-width": 1.2 / pxPerPt() }));
    }
  }

  function refresh() { for (let n = 1; n <= state.pages.length; n++) renderPage(n); positionEditor(); }

  function attach() { // called whenever the page elements are rebuilt
    closeEditor(false);
    items = items.filter((i) => i.page <= state.pages.length);
    for (const p of state.pages) {
      p.editLayer = svg("svg", { class: "edit-layer", viewBox: `0 0 ${p.w} ${p.h}`, preserveAspectRatio: "none" });
      p.div.append(p.editLayer);
    }
    refresh();
    changed();
  }

  function syncPage(n) {
    const p = state.pages[n - 1];
    p?.editLayer?.setAttribute("viewBox", `0 0 ${p.w} ${p.h}`);
  }

  // ---------- state changes ----------
  function changed() {
    const n = items.length;
    $("edit-count").textContent = n ? `${n} unsaved edit${n === 1 ? "" : "s"}` : "";
    update(enabled);
    ctx.onChange();
  }

  function update(editable) {
    enabled = editable;
    for (const b of document.querySelectorAll("#edit-tools button, #edit-insert button")) b.disabled = !editable;
    $("replace-open").disabled = !editable;
    for (const c of document.querySelectorAll("#edit-props input, #edit-props select, #edit-props button")) c.disabled = !editable;
    $("edit-delete").disabled = !editable || !find(selectedId);
    $("save-edits").disabled = $("discard-edits").disabled = !editable || !items.length;
    if (!editable && tool !== "select") setTool("select");
  }

  function setTool(name) {
    closeEditor(true);
    tool = name;
    pagesEl.dataset.tool = name;
    for (const b of document.querySelectorAll("#edit-tools button")) b.classList.toggle("active", b.dataset.tool === name);
    if (name !== "select") select(null);
  }

  function select(id) {
    const before = find(selectedId);
    selectedId = id;
    if (before) renderPage(before.page);
    const item = find(id);
    if (item) {
      renderPage(item.page);
      const color = item.color || item.stroke;
      if (color) $("edit-color").value = color;
      if (item.type === "text") {
        if (![...$("edit-size").options].some((o) => Number(o.value) === item.size)) $("edit-size").add(new Option(String(item.size), String(item.size)));
        $("edit-size").value = String(item.size);
        $("edit-font").value = item.font;
        $("edit-bold").classList.toggle("active", item.bold);
        $("edit-italic").classList.toggle("active", item.italic);
      }
      if ((item.type === "rect" || item.type === "ellipse") && !item.kind) {
        $("edit-fill-on").classList.toggle("active", Boolean(item.fill));
        if (item.fill) $("edit-fill").value = item.fill;
      }
    }
    $("edit-delete").disabled = !enabled || !item;
  }

  function add(item) {
    item.id = `e${++seq}`;
    items.push(item);
    renderPage(item.page);
    changed();
    return item;
  }

  function remove(id) {
    const item = find(id);
    if (!item) return;
    items = items.filter((i) => i !== item);
    if (selectedId === id) selectedId = null;
    renderPage(item.page);
    changed();
  }

  function applyProps() { // toolbar controls changed: restyle the selected object
    const item = find(selectedId);
    if (!item) return;
    if (item.type === "text") Object.assign(item, { color: prop.color(), size: prop.size(), font: prop.font(), bold: prop.bold(), italic: prop.italic() });
    else if (item.type === "line" || item.type === "ink") Object.assign(item, { color: prop.color(), width: prop.width() });
    else if ((item.type === "rect" || item.type === "ellipse") && !item.kind) Object.assign(item, { stroke: prop.color(), stroke_width: prop.width(), fill: prop.fill() });
    renderPage(item.page);
  }

  // ---------- pointer interaction ----------
  function toPoint(n, e, keepInside = false) {
    const p = state.pages[n - 1], rect = p.div.getBoundingClientRect();
    let x = (e.clientX - rect.left) / rect.width * p.w, y = (e.clientY - rect.top) / rect.height * p.h;
    if (keepInside) { x = clamp(x, 0, p.w); y = clamp(y, 0, p.h); }
    return { x: round(x), y: round(y) };
  }

  function newShape(n, pt) {
    const base = { page: n };
    switch (tool) {
      case "whiteout": return { ...base, type: "rect", kind: "whiteout", x: pt.x, y: pt.y, w: 0, h: 0, fill: "#ffffff", stroke: null, stroke_width: 1, opacity: 1 };
      case "highlight": return { ...base, type: "rect", kind: "highlight", x: pt.x, y: pt.y, w: 0, h: 0, fill: "#ffe600", stroke: null, stroke_width: 1, opacity: 0.4 };
      case "strike": case "underline": return { ...base, type: "rect", kind: tool, x: pt.x, y: pt.y, w: 0, h: 0, fill: prop.color(), stroke: null, stroke_width: 1, opacity: 0.15 };
      case "rect": case "ellipse": return { ...base, type: tool, x: pt.x, y: pt.y, w: 0, h: 0, fill: prop.fill(), stroke: prop.color(), stroke_width: prop.width(), opacity: 1 };
      case "line": case "arrow": return { ...base, type: "line", x1: pt.x, y1: pt.y, x2: pt.x, y2: pt.y, color: prop.color(), width: prop.width(), arrow: tool === "arrow" };
      case "link": return { ...base, type: "link", x: pt.x, y: pt.y, w: 0, h: 0, url: "" };
      case "field": return { ...base, type: "field", x: pt.x, y: pt.y, w: 0, h: 0, field: "text", name: "" };
      case "pen": return { ...base, type: "ink", points: [[pt.x, pt.y]], color: prop.color(), width: prop.width() };
      default: return null;
    }
  }

  function onDown(e) {
    if (!enabled || e.button !== 0 || e.target.closest(".edit-textbox")) return;
    const pageDiv = e.target.closest(".page");
    if (editor) { closeEditor(true); return; } // a click outside the text box just finishes typing
    if (!pageDiv) { if (tool === "select") select(null); return; }
    const n = Number(pageDiv.dataset.page);
    if (tool === "select") {
      const itemEl = e.target.closest("[data-id]");
      if (e.target.closest("[data-handle]") && find(selectedId)) {
        const item = find(selectedId);
        drag = { mode: "resize", n, item, start: toPoint(n, e), orig: structuredClone(item) };
      } else if (itemEl) {
        select(itemEl.dataset.id);
        const item = find(selectedId);
        drag = { mode: "move", n, item, start: toPoint(n, e), orig: structuredClone(item), moved: false };
      } else { select(null); return; }
    } else if (tool === "edittext") {
      e.preventDefault();
      editRunAt(n, toPoint(n, e));
      return;
    } else if (tool === "tick" || tool === "cross") {
      e.preventDefault();
      const { x, y } = toPoint(n, e, true), s = round(prop.size() * 0.45);
      const points = tool === "tick" ? [[x - s, y], [round(x - s * 0.3), round(y + s * 0.7)], [x + s, round(y - s * 0.8)]]
        : [[x - s, y - s], [x + s, y + s], [x, y], [x - s, y + s], [x + s, y - s]];
      add({ type: "ink", page: n, points, color: prop.color(), width: Math.max(1.2, prop.width()) });
      return;
    } else if (tool === "text") {
      e.preventDefault();
      const pt = toPoint(n, e, true), size = prop.size();
      openEditor(n, null, { x: pt.x, y: round(pt.y - 0.6 * size) });
      return;
    } else {
      const pt = toPoint(n, e, true), item = newShape(n, pt);
      if (!item) return;
      item.id = `e${++seq}`;
      items.push(item);
      drag = { mode: "create", n, item, start: pt };
    }
    e.preventDefault();
    try { pagesEl.setPointerCapture(e.pointerId); } catch { /* synthetic events have no active pointer */ }
  }

  function onMove(e) {
    if (!drag) return;
    const { item, orig, start, mode, n } = drag;
    const pt = toPoint(n, e, mode === "create"), dx = pt.x - start.x, dy = pt.y - start.y;
    if (mode === "create") {
      if (item.type === "ink") { const last = item.points.at(-1); if (Math.hypot(pt.x - last[0], pt.y - last[1]) >= 0.7 && item.points.length < 5000) item.points.push([pt.x, pt.y]); }
      else if (item.type === "line") Object.assign(item, { x2: pt.x, y2: pt.y });
      else Object.assign(item, { x: Math.min(start.x, pt.x), y: Math.min(start.y, pt.y), w: Math.abs(dx), h: Math.abs(dy) });
    } else if (mode === "move") {
      if (!drag.moved && Math.hypot(dx, dy) * pxPerPt() < 3) return;
      drag.moved = true;
      if (item.type === "line") Object.assign(item, { x1: round(orig.x1 + dx), y1: round(orig.y1 + dy), x2: round(orig.x2 + dx), y2: round(orig.y2 + dy) });
      else if (item.type === "ink") item.points = orig.points.map(([x, y]) => [round(x + dx), round(y + dy)]);
      else Object.assign(item, { x: round(orig.x + dx), y: round(orig.y + dy) });
    } else if (mode === "resize") {
      if (item.type === "line") Object.assign(item, { x2: pt.x, y2: pt.y });
      else if (item.type === "image") { item.w = round(Math.max(8, orig.w + dx)); item.h = round(item.w * orig.h / orig.w); }
      else if (item.type === "text") {
        const height = orig.text.split("\n").length * LINE_HEIGHT * orig.size;
        item.size = Math.round(clamp(orig.size * (1 + dy / height), 4, 400) * 2) / 2;
      } else Object.assign(item, { w: round(Math.max(4, orig.w + dx)), h: round(Math.max(4, orig.h + dy)) });
    }
    renderPage(n);
  }

  function onUp() {
    if (!drag) return;
    const { item, mode, n } = drag;
    drag = null;
    if (mode !== "create") { if (mode === "resize" && item.type === "text") select(item.id); return; }
    const tooSmall = item.type === "ink" ? item.points.length < 2
      : item.type === "line" ? Math.hypot(item.x2 - item.x1, item.y2 - item.y1) < 3
      : item.w < 3 || item.h < 3;
    if (tooSmall) { items = items.filter((i) => i !== item); renderPage(n); return; }
    if (item.kind === "strike" || item.kind === "underline") { // the dragged box marks the text line; keep only the stroke
      const y = round(item.kind === "strike" ? item.y + item.h * 0.55 : item.y + item.h);
      const line = { id: item.id, page: n, type: "line", x1: item.x, y1: y, x2: round(item.x + item.w), y2: y, color: item.fill,
                     width: round(clamp(item.h * 0.07, 0.75, 3)), arrow: false };
      items[items.indexOf(item)] = line;
      renderPage(n);
    }
    changed();
    if (!STICKY_TOOLS.has(tool)) { setTool("select"); select(item.id); }
    if (item.type === "link") askLink(item, true);
    if (item.type === "field") askField(item, true);
  }

  // ---------- text box ----------
  function positionEditor() {
    if (!editor) return;
    const { box, n, at, style } = editor, k = pxPerPt();
    Object.assign(box.style, { left: `${at.x * k}px`, top: `${at.y * k}px`, fontSize: `${style.size * k}px`, fontFamily: FAMILIES[style.font],
                               fontWeight: style.bold ? "bold" : "normal", fontStyle: style.italic ? "italic" : "normal", color: style.color });
    box.style.width = box.style.height = "0";
    box.style.width = `${Math.max(box.scrollWidth + 2, style.size * k)}px`;
    box.style.height = `${Math.max(box.scrollHeight, LINE_HEIGHT * style.size * k)}px`;
    if (!state.pages[n - 1]) closeEditor(false);
  }

  function openEditor(n, item, at) {
    closeEditor(true);
    const style = item ? { size: item.size, font: item.font, bold: item.bold, italic: item.italic, color: item.color }
      : { size: prop.size(), font: prop.font(), bold: prop.bold(), italic: prop.italic(), color: prop.color() };
    const box = Object.assign(document.createElement("textarea"), { className: "edit-textbox", value: item ? item.text : "", spellcheck: false, wrap: "off", rows: 1 });
    box.setAttribute("aria-label", "Text to add to the page");
    editor = { box, n, item, at: item ? { x: item.x, y: item.y } : at, style };
    state.pages[n - 1].div.append(box);
    if (item) renderPage(n); // hide the object while its text is being edited
    box.addEventListener("input", positionEditor);
    box.addEventListener("blur", () => closeEditor(true));
    box.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Escape") { e.preventDefault(); closeEditor(Boolean(item)); }
      else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); closeEditor(true); }
    });
    positionEditor();
    box.focus();
  }

  function closeEditor(keep) {
    if (!editor) return;
    const { box, n, item, at, style } = editor;
    editor = null;
    box.remove();
    const text = box.value.replace(/\r\n?/g, "\n").replace(/\t/g, "    ").replace(/[\x00-\x09\x0B-\x1F]/g, "").replace(/\s+$/, "").slice(0, 5000);
    if (item) {
      if (keep && !text) return remove(item.id);
      if (keep) item.text = text;
      renderPage(n);
      if (keep) changed();
    } else if (keep && text.trim()) {
      const created = add({ type: "text", page: n, x: at.x, y: at.y, text, ...style });
      setTool("select");
      select(created.id);
    }
  }

  // ---------- links ----------
  let linking = null; // { item, isNew } while the address dialog is open
  function askLink(item, isNew) {
    linking = { item, isNew };
    $("link-url").value = item.url;
    $("link-dialog").returnValue = "cancel";
    $("link-dialog").showModal();
    $("link-url").focus();
  }
  function setUpLinks() {
    $("link-dialog").addEventListener("close", () => {
      if (!linking) return;
      const { item, isNew } = linking;
      linking = null;
      let url = $("link-url").value.trim();
      if ($("link-dialog").returnValue !== "ok") { if (isNew) remove(item.id); return; }
      if (/^[^\s@:/]+@[^\s@/]+\.[^\s@/]+$/.test(url)) url = `mailto:${url}`;
      else if (url && !/^[a-z][a-z0-9+.-]*:/i.test(url)) url = `https://${url}`;
      if (!/^(https?:\/\/|mailto:)[^\s<>]+$/i.test(url) || url.length > 2000) {
        toast("Enter a web address such as https://example.com or an email address");
        if (isNew) remove(item.id);
        return;
      }
      item.url = url;
      renderPage(item.page);
      changed();
    });
  }

  // ---------- new form fields ----------
  let naming = null;
  function askField(item, isNew) {
    naming = { item, isNew };
    $("field-name").value = item.name || `Field ${items.filter((i) => i.type === "field").length}`;
    $("field-type").value = isNew && item.w < 30 && item.h < 30 ? "checkbox" : item.field; // a small square is probably a check box
    $("field-dialog").returnValue = "cancel";
    $("field-dialog").showModal();
    $("field-name").select();
  }
  function setUpFields() {
    $("field-dialog").addEventListener("close", () => {
      if (!naming) return;
      const { item, isNew } = naming;
      naming = null;
      const name = $("field-name").value.trim().replace(/\s+/g, " ");
      if ($("field-dialog").returnValue !== "ok") { if (isNew) remove(item.id); return; }
      if (!/^[A-Za-z0-9][A-Za-z0-9 _-]{0,59}$/.test(name)) {
        toast("Field names can use letters, numbers, spaces, - and _ (up to 60 characters)");
        if (isNew) remove(item.id);
        return;
      }
      Object.assign(item, { name, field: $("field-type").value });
      renderPage(item.page);
      changed();
    });
  }

  // ---------- changing text that is already in the PDF ----------
  // The original run is covered with a box in the page's background colour and retyped on top as a new text object.
  function sampleColours(n, run) {
    const fallback = { bg: "#ffffff", fg: "#000000" };
    const p = state.pages[n - 1], canvas = p?.canvas;
    if (!canvas?.width) return fallback;
    try {
      const k = canvas.width / p.w;
      const x0 = clamp(Math.floor((run.x - 1) * k), 0, canvas.width - 1), y0 = clamp(Math.floor(run.y * k), 0, canvas.height - 1);
      const w = clamp(Math.ceil((run.w + 2) * k), 1, canvas.width - x0), h = clamp(Math.ceil(run.h * k), 1, canvas.height - y0);
      if (w * h > 4_000_000) return fallback;
      const d = canvas.getContext("2d", { alpha: false }).getImageData(x0, y0, w, h).data;
      const tally = new Map();
      for (const row of [0, h - 1]) for (let x = 0; x < w; x++) { // the background is the commonest colour along the top and bottom edges
        const i = (row * w + x) * 4, key = (d[i] >> 2 << 16) | (d[i + 1] >> 2 << 8) | (d[i + 2] >> 2);
        const e = tally.get(key) || { n: 0, r: d[i], g: d[i + 1], b: d[i + 2] };
        e.n++; tally.set(key, e);
      }
      const bg = [...tally.values()].sort((a, b) => b.n - a.n)[0];
      let far = 0, fg = null;
      for (let i = 0; i < d.length; i += 4) { // the text colour is the pixel that differs most from the background
        const dist = Math.abs(d[i] - bg.r) + Math.abs(d[i + 1] - bg.g) + Math.abs(d[i + 2] - bg.b);
        if (dist > far) { far = dist; fg = [d[i], d[i + 1], d[i + 2]]; }
      }
      return { bg: hex(bg.r, bg.g, bg.b), fg: far > 90 ? hex(...fg) : fallback.fg };
    } catch { return fallback; }
  }

  function replaceRun(n, run, text) {
    const { bg, fg } = sampleColours(n, run);
    const cover = add({ type: "rect", kind: "whiteout", page: n, x: round(run.x - 1), y: round(run.y), w: round(run.w + 2), h: round(run.h),
                        fill: bg, stroke: null, stroke_width: 1, opacity: 1 });
    if (!text.trim()) return cover;
    return add({ type: "text", page: n, x: round(run.x), y: round(run.baseline - ASCENT * run.size), text,
                 size: clamp(Math.round(run.size * 2) / 2, 4, 400), color: fg, font: run.font, bold: run.bold, italic: run.italic });
  }

  async function editRunAt(n, pt) {
    let runs;
    try { runs = await ctx.textRuns(n); } catch { runs = []; }
    const run = runs.findLast((r) => r.upright && pt.x >= r.x - 2 && pt.x <= r.x + r.w + 2 && pt.y >= r.y - 2 && pt.y <= r.y + r.h + 2);
    if (!run) return toast("Click on a line of text to change it. Scanned pages and pictures of text have no editable text: cover them with Whiteout and use Text instead.");
    const item = replaceRun(n, run, run.str);
    setTool("select");
    select(item.id);
    openEditor(n, item);
  }

  async function replaceAll(find, replacement) {
    const pattern = new RegExp(find.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
    let count = 0, places = 0;
    for (let n = 1; n <= state.pages.length; n++) {
      for (const run of await ctx.textRuns(n)) {
        const hits = run.upright ? (run.str.match(pattern) || []).length : 0;
        if (!hits) continue;
        replaceRun(n, run, run.str.replace(pattern, () => replacement));
        count += hits; places++;
      }
    }
    return { count, places };
  }

  function setUpReplace() {
    const dialog = $("replace-dialog");
    $("replace-open").addEventListener("click", () => {
      $("replace-find").value = $("search").value.trim();
      dialog.returnValue = "cancel";
      dialog.showModal();
      $("replace-find").focus();
    });
    dialog.addEventListener("close", async () => {
      const find = $("replace-find").value.trim();
      if (dialog.returnValue !== "ok" || !find || !enabled) return;
      try {
        const { count, places } = await replaceAll(find, $("replace-with").value);
        if (!count) return toast(`"${find}" was not found in the document's text`);
        toast(`Replaced ${count} occurrence${count === 1 ? "" : "s"} in ${places} place${places === 1 ? "" : "s"}. Check the pages, then click Save edits.`, "ok");
      } catch (e) { console.error(e); toast("Find and replace could not read this document's text"); }
    });
  }

  // ---------- images & signatures ----------
  function place(dataUrl, pxW, pxH, wantedWidth) {
    const n = state.current, p = state.pages[n - 1];
    if (!p) return;
    let w = Math.min(wantedWidth, p.w * 0.6), h = w * pxH / pxW;
    if (h > p.h * 0.6) { h = p.h * 0.6; w = h * pxW / pxH; }
    const view = viewer.getBoundingClientRect();
    const centre = toPoint(n, { clientX: view.left + view.width / 2, clientY: view.top + view.height / 2 });
    const item = add({ type: "image", page: n, data: dataUrl, w: round(w), h: round(h),
                       x: round(clamp(centre.x - w / 2, 0, Math.max(0, p.w - w))), y: round(clamp(centre.y - h / 2, 0, Math.max(0, p.h - h))) });
    setTool("select");
    select(item.id);
  }

  async function insertImageFile(file, wantedWidth = 0) {
    const url = URL.createObjectURL(file);
    try {
      const img = await loadImage(url);
      const scale = Math.min(1, MAX_IMAGE_SIDE / Math.max(img.naturalWidth, img.naturalHeight));
      const canvas = Object.assign(document.createElement("canvas"), { width: Math.max(1, Math.round(img.naturalWidth * scale)), height: Math.max(1, Math.round(img.naturalHeight * scale)) });
      canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      const jpeg = file.type === "image/jpeg";
      place(canvas.toDataURL(jpeg ? "image/jpeg" : "image/png", 0.9), canvas.width, canvas.height, wantedWidth || canvas.width * 0.75);
    } catch (e) { toast(e.message || "This image could not be added"); }
    finally { URL.revokeObjectURL(url); }
  }

  function setUpSignaturePad() {
    const dialog = $("sign-dialog"), canvas = $("sign-canvas"), pen = canvas.getContext("2d");
    let drawing = false, box = null;
    const clear = () => { pen.clearRect(0, 0, canvas.width, canvas.height); box = null; };
    const at = (e) => { const r = canvas.getBoundingClientRect(); return [(e.clientX - r.left) * canvas.width / r.width, (e.clientY - r.top) * canvas.height / r.height]; };
    const grow = (x, y) => { box = box ? [Math.min(box[0], x), Math.min(box[1], y), Math.max(box[2], x), Math.max(box[3], y)] : [x, y, x, y]; };
    canvas.addEventListener("pointerdown", (e) => {
      drawing = true;
      try { canvas.setPointerCapture(e.pointerId); } catch { /* ignore */ }
      const [x, y] = at(e);
      Object.assign(pen, { lineWidth: 5, lineCap: "round", lineJoin: "round", strokeStyle: "#0b1f6b" });
      pen.beginPath(); pen.moveTo(x, y); pen.lineTo(x + 0.1, y + 0.1); pen.stroke(); grow(x, y);
    });
    canvas.addEventListener("pointermove", (e) => { if (!drawing) return; const [x, y] = at(e); pen.lineTo(x, y); pen.stroke(); grow(x, y); });
    for (const type of ["pointerup", "pointercancel"]) canvas.addEventListener(type, () => { drawing = false; });
    $("sign-clear").addEventListener("click", clear);
    $("edit-sign").addEventListener("click", () => { clear(); $("sign-name").value = ""; dialog.returnValue = "cancel"; dialog.showModal(); });
    $("sign-upload").addEventListener("click", () => $("sign-upload-input").click());
    $("sign-upload-input").addEventListener("change", (e) => {
      const file = e.target.files[0];
      e.target.value = "";
      if (!file) return;
      dialog.close("cancel");
      insertImageFile(file, 160);
    });
    dialog.addEventListener("close", () => {
      if (dialog.returnValue !== "ok") return;
      const name = $("sign-name").value.trim();
      if (!box && name) { // typed signature in a handwriting-style font
        const font = SIGNATURE_FONTS[Number($("sign-style").value)] || SIGNATURE_FONTS[0];
        const probe = document.createElement("canvas").getContext("2d");
        probe.font = font;
        const tw = Math.min(4000, Math.ceil(probe.measureText(name).width) + 40), th = 150;
        const typed = Object.assign(document.createElement("canvas"), { width: tw, height: th });
        const ink = typed.getContext("2d");
        Object.assign(ink, { font, fillStyle: "#0b1f6b", textBaseline: "alphabetic" });
        ink.fillText(name, 20, 105);
        return place(typed.toDataURL("image/png"), tw, th, Math.min(260, tw * 0.3));
      }
      if (!box) return toast("Draw your signature or type your name first");
      const pad = 8, x = Math.max(0, Math.floor(box[0] - pad)), y = Math.max(0, Math.floor(box[1] - pad));
      const w = Math.min(canvas.width, Math.ceil(box[2] + pad)) - x, h = Math.min(canvas.height, Math.ceil(box[3] + pad)) - y;
      const out = Object.assign(document.createElement("canvas"), { width: w, height: h });
      out.getContext("2d").drawImage(canvas, x, y, w, h, 0, 0, w, h);
      place(out.toDataURL("image/png"), w, h, Math.max(40, w * 0.36)); // drawn at 2x; 0.36 keeps roughly the size it was written
    });
  }

  // ---------- saving ----------
  function textAsImage(item) { // text the standard PDF fonts cannot encode: draw it with the browser's fonts
    const k = 4, lines = item.text.split("\n");
    const font = `${item.italic ? "italic " : ""}${item.bold ? "bold " : ""}${item.size * k}px ${FAMILIES[item.font]}`;
    const probe = document.createElement("canvas").getContext("2d");
    probe.font = font;
    const width = Math.ceil(Math.max(...lines.map((l) => probe.measureText(l).width), 1)) + 2 * k;
    const height = Math.ceil((lines.length * LINE_HEIGHT + 0.3) * item.size * k);
    if (width > 16000 || height > 16000) throw new Error("This text is too large to save; split it into smaller pieces");
    const canvas = Object.assign(document.createElement("canvas"), { width, height });
    const pen = canvas.getContext("2d");
    Object.assign(pen, { font, fillStyle: item.color, textBaseline: "alphabetic" });
    lines.forEach((line, i) => pen.fillText(line, 0, (ASCENT + i * LINE_HEIGHT) * item.size * k));
    return { type: "image", page: item.page, x: item.x, y: item.y, w: round(width / k), h: round(height / k), data: canvas.toDataURL("image/png") };
  }

  function buildPayload() {
    return items.map((item) => {
      if (item.type === "text" && !CP1252.test(item.text)) return textAsImage(item);
      const { id, kind, cover, ...rest } = item;
      return rest;
    });
  }

  function undoLocal() { // undo for unsaved work: take back the most recent object
    closeEditor(false);
    const last = items.at(-1);
    if (last) remove(last.id);
  }

  function clear() {
    closeEditor(false);
    items = []; selectedId = null;
    refresh();
    changed();
  }

  // ---------- wiring ----------
  pagesEl.addEventListener("pointerdown", onDown);
  pagesEl.addEventListener("pointermove", onMove);
  pagesEl.addEventListener("pointerup", onUp);
  pagesEl.addEventListener("pointercancel", onUp);
  pagesEl.addEventListener("dblclick", (e) => {
    const item = enabled && tool === "select" ? find(e.target.closest("[data-id]")?.dataset.id) : null;
    if (item?.type === "text") openEditor(item.page, item);
    else if (item?.type === "link") askLink(item, false);
    else if (item?.type === "field") askField(item, false);
  });
  for (const b of document.querySelectorAll("#edit-tools button")) b.addEventListener("click", () => setTool(b.dataset.tool));
  for (const id of ["edit-color", "edit-size", "edit-width", "edit-font", "edit-fill"]) $(id).addEventListener("input", applyProps);
  for (const id of ["edit-bold", "edit-italic", "edit-fill-on"]) $(id).addEventListener("click", () => { $(id).classList.toggle("active"); applyProps(); });
  $("edit-delete").addEventListener("click", () => remove(selectedId));
  $("edit-image").addEventListener("click", () => $("edit-image-input").click());
  $("edit-image-input").addEventListener("change", (e) => { const file = e.target.files[0]; e.target.value = ""; if (file) insertImageFile(file); });
  $("save-edits").addEventListener("click", () => ctx.save());
  $("discard-edits").addEventListener("click", clear);
  setUpSignaturePad();
  setUpReplace();
  setUpLinks();
  setUpFields();
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); if (items.length) { closeEditor(true); ctx.save(); } }
    else if (typing || !enabled) return;
    else if ((e.key === "Delete" || e.key === "Backspace") && find(selectedId)) { e.preventDefault(); remove(selectedId); }
    else if (e.key === "Escape") { if (tool !== "select") setTool("select"); else select(null); }
  });
  setTool("select");

  return { attach, syncPage, refresh, update, clear, undoLocal, buildPayload, hasPending: () => items.length > 0,
           finishTyping: () => closeEditor(true) };
}
