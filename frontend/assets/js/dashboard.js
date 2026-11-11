import { api, formatBytes, logout, requireUser, toast, uploadPdf } from "./api.js";

const $ = (id) => document.getElementById(id);
const PAGE = 50;
let loaded = 0;

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function row(doc) {
  const open = el("a", { className: "btn btn-sm btn-brand", href: `editor.html?id=${encodeURIComponent(doc.id)}`, textContent: "Open" });
  const download = el("button", { className: "btn btn-sm btn-outline-secondary", type: "button", textContent: "Download" });
  const remove = el("button", { className: "btn btn-sm btn-outline-danger", type: "button", textContent: "Delete" });
  const name = el("a", { className: "file-name", href: open.href, textContent: doc.original_filename, title: doc.original_filename });
  const badge = doc.processing_status === "encrypted" ? el("span", { className: "badge text-bg-warning ms-2", textContent: "Password protected" }) : "";
  const tr = el("tr", {},
    el("td", {}, name, badge),
    el("td", { className: "num", textContent: doc.page_count ?? "—" }),
    el("td", { className: "num", textContent: formatBytes(doc.file_size) }),
    el("td", { textContent: new Date(doc.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) }),
    el("td", { className: "actions" }, open, download, remove));
  tr.dataset.id = doc.id;

  download.addEventListener("click", async () => {
    download.disabled = true;
    try {
      const res = await api(`/api/documents/${doc.id}/download`, { raw: true });
      saveBlob(await res.blob(), doc.original_filename);
    } catch (e) { toast(e.message); }
    finally { download.disabled = false; }
  });

  remove.addEventListener("click", () => {
    const dialog = $("confirm-delete");
    $("delete-name").textContent = doc.original_filename;
    dialog.returnValue = "cancel";
    dialog.onclose = async () => {
      if (dialog.returnValue !== "delete") return;
      try {
        await api(`/api/documents/${doc.id}`, { method: "DELETE" });
        toast("Document deleted", "ok");
        await reload();
      } catch (e) { toast(e.message); }
    };
    dialog.showModal();
  });
  return tr;
}

async function loadMore() {
  const data = await api(`/api/documents?limit=${PAGE}&offset=${loaded}`);
  $("file-rows").append(...data.items.map(row));
  loaded += data.items.length;
  $("file-count").textContent = data.total ? `${data.total} document${data.total === 1 ? "" : "s"}` : "";
  $("empty").hidden = data.total > 0;
  $("files").hidden = data.total === 0;
  $("more").hidden = loaded >= data.total;
}

async function reload() {
  loaded = 0;
  $("file-rows").replaceChildren();
  await loadMore();
}

async function uploadAll(fileList) {
  const files = [...fileList];
  for (const file of files) {
    const bar = el("div", { className: "bar" });
    const status = el("span", { className: "status", textContent: "Uploading…" });
    const item = el("li", {}, el("span", { className: "name", textContent: file.name }), el("div", { className: "track" }, bar), status);
    $("uploads").append(item);
    try {
      await uploadPdf(file, (p) => { bar.style.width = `${Math.round(p * 100)}%`; if (p === 1) status.textContent = "Checking file…"; });
      bar.style.width = "100%";
      status.textContent = "Done";
      item.classList.add("done");
      setTimeout(() => item.remove(), 2500);
    } catch (e) {
      status.textContent = e.message;
      item.classList.add("failed");
      setTimeout(() => item.remove(), 10_000);
    }
  }
  await reload().catch((e) => toast(e.message));
}

const zone = $("dropzone");
$("choose").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (e) => { uploadAll(e.target.files); e.target.value = ""; });
for (const type of ["dragenter", "dragover"]) zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("over"); });
for (const type of ["dragleave", "drop"]) zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.remove("over"); });
zone.addEventListener("drop", (e) => { if (e.dataTransfer?.files.length) uploadAll(e.dataTransfer.files); });
// Dropping a file outside the zone must not navigate the browser away to it.
for (const type of ["dragover", "drop"]) window.addEventListener(type, (e) => e.preventDefault());
$("more").addEventListener("click", () => loadMore().catch((e) => toast(e.message)));
$("logout").addEventListener("click", async () => { await logout(); location.replace("index.html"); });

const user = await requireUser();
$("user-name").textContent = user.full_name;
reload().catch((e) => toast(e.message));
api("/api/health").then((h) => { $("upload-limit").textContent = `Up to ${h.max_upload_mb} MB per file.`; }).catch(() => {});
