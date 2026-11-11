"""PDF tools. Merge, split, compress, protect, unlock and images-to-PDF create new documents and leave the
sources untouched; crop, metadata, page numbers, headers/footers, watermark and form filling add a version
to the document (undo with /api/editor/{id}/undo); PDF-to-images and PDF-to-text are downloads."""
from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..config import get_settings
from ..database import get_db
from ..errors import ApiError
from ..models import Document, DocumentVersion, User
from ..schemas.document import DocumentOut
from ..schemas.tools import (BatesRequest, CompressRequest, CropRequest, FormFillRequest, GrayscaleRequest, MergeRequest,
                             MetadataRequest, MixRequest, NupRequest, PasswordRequest, ProtectRequest,
                             RemoveAnnotationsRequest, ResizeRequest, SplitRequest, StampRequest, ToImagesRequest,
                             WatermarkRequest, _Doc)
from ..security.deps import client_ip, get_current_user
from ..services import audit, storage, versions
from ..services.pdf_inspect import inspect_pdf
from ..services.sandbox import run_worker
from .documents import get_owned_document

router = APIRouter(prefix="/api/tools", tags=["PDF Tools"])
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 30
MAX_IMAGE_PAGES = 100


def _tmp(suffix: str) -> Path:
    return get_settings().temp_dir / f"{uuid.uuid4().hex}.{suffix}"


def _source(doc: Document, allow_encrypted: bool = False) -> Path:
    if not allow_encrypted and (doc.page_count is None or doc.processing_status == "encrypted"):
        raise ApiError(409, "document_encrypted", "Password-protected PDFs must be unlocked first (use the Unlock tool)")
    path = storage.resolve(doc.storage_path)
    if not path.is_file():
        raise ApiError(410, "file_missing", "The stored file is no longer available")
    return path


def _run(operation: str, request: dict) -> dict:
    result = run_worker("tool_ops.py", [operation], stdin=json.dumps(request))
    if result.get("ok"):
        return result
    error, detail = result.get("error"), result.get("detail") or ""
    if error == "encrypted":
        raise ApiError(409, "document_encrypted", "Encrypted PDFs must be unlocked first (use the Unlock tool)")
    if error == "wrong_password":
        raise ApiError(403, "wrong_password", "That is not the right password for this document")
    if error == "bad_request":
        raise ApiError(422, "invalid_request", detail[:1].upper() + detail[1:] if detail else "The request could not be carried out")
    raise ApiError(422, "tool_failed", "This PDF could not be processed; its structure may be damaged")


def _pages(spec: str, count: int) -> list[int]:
    """'all' or '1-3, 5' -> sorted unique 1-based page numbers."""
    if spec.strip().lower() == "all":
        return list(range(1, count + 1))
    pages: set[int] = set()
    for part in spec.split(","):
        first, _, last = part.partition("-")
        a, b = int(first), int(last or first)
        if not 1 <= a <= b <= count:
            raise ApiError(422, "invalid_pages", f"The document has only {count} pages")
        pages.update(range(a, b + 1))
    return sorted(pages)


def _stem(doc: Document) -> str:
    return PurePosixPath(doc.original_filename).stem[:180]


def _new_document(db: Session, user: User, request: Request, tmp: Path, name: str, description: str, action: str) -> Document:
    """Store a tool's output file as a new document of the user (validated like an upload)."""
    try:
        info = inspect_pdf(tmp)
        size = tmp.stat().st_size
        stored, rel = storage.commit_upload(tmp, user.id)
    finally:
        tmp.unlink(missing_ok=True)
    doc = Document(user_id=user.id, original_filename=storage.sanitize_filename(name), stored_filename=stored, file_size=size,
                   page_count=info["page_count"], mime_type="application/pdf", storage_path=rel,
                   processing_status="encrypted" if info["needs_password"] else "ready")
    doc.versions.append(DocumentVersion(version_number=1, storage_path=rel, change_description=description[:500],
                                        page_count=info["page_count"], file_size=size))
    db.add(doc)
    try:
        db.flush()
        audit.record(db, action, user.id, doc.id, client_ip(request))
        db.commit()
    except Exception:
        db.rollback()
        storage.secure_delete(rel)
        raise
    return doc


def _new_version(db: Session, user: User, request: Request, doc: Document, tmp: Path, page_count: int, description: str,
                 action: str) -> Document:
    try:
        size = tmp.stat().st_size
        _, rel = storage.commit_upload(tmp, doc.user_id)
    finally:
        tmp.unlink(missing_ok=True)
    return _record_version(db, user, request, doc, rel, size, page_count, description, action)


def _record_version(db, user, request, doc, rel, size, page_count, description, action) -> Document:
    try:
        doomed = versions.add_version(db, doc, rel, size, page_count, description)
        audit.record(db, action, user.id, doc.id, client_ip(request))
        db.commit()
    except Exception:
        db.rollback()
        storage.secure_delete(rel)
        raise
    for path in doomed:
        storage.secure_delete(path)
    return doc


def _download(path: Path, filename: str, media_type: str) -> FileResponse:
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    return FileResponse(path, media_type=media_type, background=BackgroundTask(path.unlink, missing_ok=True), headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
        "X-Content-Type-Options": "nosniff"})


# ---------- tools that create new documents ----------
@router.post("/merge", response_model=DocumentOut, status_code=201)
def merge(body: MergeRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    docs = [get_owned_document(i, db, user) for i in body.document_ids]
    out = _tmp("out")
    _run("merge", {"inputs": [str(_source(d)) for d in docs], "out": str(out), "max_pages": get_settings().max_pages})
    return _new_document(db, user, request, out, body.name, f"Merged from {len(docs)} documents", "tools.merge")


@router.post("/split", status_code=201)
def split(body: SplitRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    src, count = _source(doc), doc.page_count
    if body.mode == "bookmarks":
        groups = [[i + 1 for i in g] for g in _run("bookmark_groups", {"inputs": [str(src)]})["groups"]]
    elif body.mode == "every":
        groups = [list(range(i, min(i + body.every - 1, count) + 1)) for i in range(1, count + 1, body.every)]
    else:
        parts = [p for p in body.ranges.split(",") if p.strip()]
        if not parts:
            raise ApiError(422, "invalid_pages", 'Give the page groups to split into, e.g. "1-3, 4-6"')
        try:
            groups = [_pages(p, count) for p in parts]
        except ValueError:
            raise ApiError(422, "invalid_pages", 'Page groups must look like "1-3, 4-6, 9"')
    if len(groups) > 50:
        raise ApiError(422, "too_many_parts", "A split can create at most 50 documents at a time")
    outs = [_tmp("out") for _ in groups]
    try:
        _run("split", {"inputs": [str(src)], "groups": [[n - 1 for n in g] for g in groups], "outs": [str(o) for o in outs]})
        created = []
        for group, out in zip(groups, outs):
            label = f"{group[0]}-{group[-1]}" if len(group) > 1 else str(group[0])
            created.append(_new_document(db, user, request, out, f"{_stem(doc)} (pages {label}).pdf",
                                         f"Split from {doc.original_filename}", "tools.split"))
    finally:
        for out in outs:
            out.unlink(missing_ok=True)
    return {"documents": [DocumentOut.model_validate(d) for d in created]}


@router.post("/compress")
def compress(body: CompressRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    src, out = _source(doc), _tmp("out")
    settings = {"lossless": {}, "balanced": {"image_quality": 70, "max_image_side": 2400},
                "strong": {"image_quality": 45, "max_image_side": 1600}}[body.level]
    _run("compress", {"inputs": [str(src)], "out": str(out), **settings})
    before, after = src.stat().st_size, out.stat().st_size
    if after >= before * 0.98:   # nothing worth keeping
        out.unlink(missing_ok=True)
        return {"document": None, "before": before, "after": before}
    new = _new_document(db, user, request, out, f"{_stem(doc)} (compressed).pdf", f"Compressed copy of {doc.original_filename}",
                        "tools.compress")
    return {"document": DocumentOut.model_validate(new), "before": before, "after": after}


@router.post("/protect", response_model=DocumentOut, status_code=201)
def protect(body: ProtectRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    out = _tmp("out")
    _run("protect", {"inputs": [str(_source(doc))], "out": str(out), "password": body.password})
    return _new_document(db, user, request, out, f"{_stem(doc)} (protected).pdf", f"Password-protected copy of {doc.original_filename}",
                         "tools.protect")


@router.post("/unlock", response_model=DocumentOut, status_code=201)
def unlock(body: PasswordRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Remove the password from a document the user can already open with that password. Passwords are never guessed."""
    doc = get_owned_document(body.document_id, db, user)
    out = _tmp("out")
    _run("unlock", {"inputs": [str(_source(doc, allow_encrypted=True))], "out": str(out), "password": body.password})
    return _new_document(db, user, request, out, f"{_stem(doc)} (unlocked).pdf", f"Unlocked copy of {doc.original_filename}",
                         "tools.unlock")


@router.post("/images-to-pdf", response_model=DocumentOut, status_code=201)
def images_to_pdf(request: Request, files: list[UploadFile] = File(...), page_size: str = Form("fit"),
                  name: str = Form("Images.pdf"), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not 1 <= len(files) <= MAX_IMAGES:
        raise ApiError(422, "invalid_request", f"Choose between 1 and {MAX_IMAGES} images")
    images = []
    for f in files:
        data = f.file.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ApiError(413, "file_too_large", "Each image can be at most 10 MB")
        images.append(base64.b64encode(data).decode())
    out = _tmp("out")
    _run("images_to_pdf", {"images": images, "out": str(out), "page_size": "a4" if page_size == "a4" else "fit"})
    return _new_document(db, user, request, out, name, f"Created from {len(images)} images", "tools.images_to_pdf")


# ---------- downloads ----------
@router.post("/to-images")
def to_images(body: ToImagesRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    out = _tmp("zip")
    try:
        _run("to_images", {"inputs": [str(_source(doc))], "out": str(out), "dpi": body.dpi, "format": body.format,
                           "max_pages": MAX_IMAGE_PAGES})
    except Exception:
        out.unlink(missing_ok=True)
        raise
    audit.record(db, "tools.to_images", user.id, doc.id, client_ip(request))
    db.commit()
    return _download(out, f"{_stem(doc)} ({body.format}).zip", "application/zip")


@router.post("/to-text")
def to_text(body: _Doc, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    out = _tmp("txt")
    try:
        _run("to_text", {"inputs": [str(_source(doc))], "out": str(out)})
    except Exception:
        out.unlink(missing_ok=True)
        raise
    audit.record(db, "tools.to_text", user.id, doc.id, client_ip(request))
    db.commit()
    return _download(out, f"{_stem(doc)}.txt", "text/plain; charset=utf-8")


# ---------- tools that add a version to the document ----------
@router.post("/crop", response_model=DocumentOut)
def crop(body: CropRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    src, out = _source(doc), _tmp("out")
    if not (body.left or body.top or body.right or body.bottom):
        raise ApiError(422, "invalid_request", "Give at least one margin to cut off")
    pages = _pages(body.pages, doc.page_count)
    result = _run("crop", {"inputs": [str(src)], "out": str(out), "pages": pages,
                           "left": body.left, "top": body.top, "right": body.right, "bottom": body.bottom})
    return _new_version(db, user, request, doc, out, result["page_count"], f"Cropped {len(pages)} page(s)", "tools.crop")


@router.post("/metadata", response_model=DocumentOut)
def metadata(body: MetadataRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    src, out = _source(doc), _tmp("out")
    values = {k: getattr(body, k) for k in ("title", "author", "subject", "keywords")}
    result = _run("metadata", {"inputs": [str(src)], "out": str(out), "values": values})
    return _new_version(db, user, request, doc, out, result["page_count"], "Edited document properties", "tools.metadata")


def _sizes(doc: Document) -> list[tuple[float, float]]:
    """Displayed (width, height) of every page, in points."""
    info = inspect_pdf(_source(doc))
    return [(p["width"], p["height"]) if p["rotation"] % 180 == 0 else (p["height"], p["width"]) for p in info["pages"]]


def _overlay(db, user, request, doc, items, description, action) -> Document:
    rel, size, page_count = versions.run_overlay(doc, items)
    return _record_version(db, user, request, doc, rel, size, page_count, description, action)


@router.post("/stamp", response_model=DocumentOut)
def stamp(body: StampRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    sizes = _sizes(doc)
    pages = _pages(body.pages, len(sizes))
    vertical, _, horizontal = body.position.partition("-")
    items = []
    for offset, n in enumerate(pages):
        width, height = sizes[n - 1]
        text = body.text.replace("{n}", str(body.start + offset)).replace("{total}", str(body.start + len(pages) - 1))
        x = {"left": body.margin, "center": width / 2, "right": width - body.margin}[horizontal]
        y = body.margin if vertical == "top" else height - body.margin - 1.2 * body.size
        items.append({"type": "text", "page": n, "x": x, "y": y, "text": text, "size": body.size, "color": body.color,
                      "font": "helvetica", "align": horizontal})
    return _overlay(db, user, request, doc, items, f"Added header/footer text to {len(pages)} page(s)", "tools.stamp")


@router.post("/watermark", response_model=DocumentOut)
def watermark(body: WatermarkRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    sizes = _sizes(doc)
    pages = _pages(body.pages, len(sizes))
    items = [{"type": "text", "page": n, "x": sizes[n - 1][0] / 2, "y": sizes[n - 1][1] / 2 - 0.55 * body.size, "text": body.text,
              "size": body.size, "color": body.color, "font": "helvetica", "bold": True, "align": "center",
              "rotate": body.angle, "opacity": body.opacity} for n in pages]
    return _overlay(db, user, request, doc, items, f"Added a watermark to {len(pages)} page(s)", "tools.watermark")


@router.post("/bates", response_model=DocumentOut)
def bates(body: BatesRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    sizes = _sizes(doc)
    pages = _pages(body.pages, len(sizes))
    vertical, _, horizontal = body.position.partition("-")
    margin, items = 24, []
    for offset, n in enumerate(pages):
        width, height = sizes[n - 1]
        x = {"left": margin, "center": width / 2, "right": width - margin}[horizontal]
        y = margin if vertical == "top" else height - margin - 1.2 * body.size
        items.append({"type": "text", "page": n, "x": x, "y": y, "text": f"{body.prefix}{body.start + offset:0{body.digits}d}",
                      "size": body.size, "color": "#000000", "font": "courier", "align": horizontal})
    return _overlay(db, user, request, doc, items, f"Added Bates numbers to {len(pages)} page(s)", "tools.bates")


@router.post("/resize", response_model=DocumentOut)
def resize(body: ResizeRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    src, out = _source(doc), _tmp("out")
    result = _run("resize", {"inputs": [str(src)], "out": str(out), "size": body.size})
    return _new_version(db, user, request, doc, out, result["page_count"], f"Resized pages to {body.size.upper()}", "tools.resize")


@router.post("/flatten", response_model=DocumentOut)
def flatten(body: _Doc, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    src, out = _source(doc), _tmp("out")
    result = _run("flatten", {"inputs": [str(src)], "out": str(out)})
    return _new_version(db, user, request, doc, out, result["page_count"], "Flattened form fields and annotations", "tools.flatten")


@router.post("/remove-annotations", response_model=DocumentOut)
def remove_annotations(body: RemoveAnnotationsRequest, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    src, out = _source(doc), _tmp("out")
    try:
        result = _run("remove_annotations", {"inputs": [str(src)], "out": str(out), "what": body.what})
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return _new_version(db, user, request, doc, out, result["page_count"], f"Removed {result['removed']} annotation(s)",
                        "tools.remove_annotations")


@router.post("/nup", response_model=DocumentOut, status_code=201)
def nup(body: NupRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    out = _tmp("out")
    _run("nup", {"inputs": [str(_source(doc))], "out": str(out), "per_sheet": body.per_sheet})
    return _new_document(db, user, request, out, f"{_stem(doc)} ({body.per_sheet}-up).pdf",
                         f"{body.per_sheet} pages per sheet from {doc.original_filename}", "tools.nup")


@router.post("/grayscale", response_model=DocumentOut, status_code=201)
def grayscale(body: GrayscaleRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    out = _tmp("out")
    _run("grayscale", {"inputs": [str(_source(doc))], "out": str(out), "dpi": body.dpi, "max_pages": MAX_IMAGE_PAGES})
    return _new_document(db, user, request, out, f"{_stem(doc)} (grayscale).pdf", f"Grayscale copy of {doc.original_filename}",
                         "tools.grayscale")


@router.post("/mix", response_model=DocumentOut, status_code=201)
def mix(body: MixRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    docs = [get_owned_document(i, db, user) for i in body.document_ids]
    out = _tmp("out")
    _run("mix", {"inputs": [str(_source(d)) for d in docs], "out": str(out), "max_pages": get_settings().max_pages})
    return _new_document(db, user, request, out, body.name, "Pages alternated from two documents", "tools.mix")


@router.post("/repair", response_model=DocumentOut, status_code=201)
def repair(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Upload a PDF that the normal upload rejects as damaged; it is rebuilt and stored if that succeeds."""
    s = get_settings()
    tmp, _ = storage.save_upload_to_temp(file.file, s.max_upload_bytes)
    out = _tmp("out")
    try:
        _run("repair", {"inputs": [str(tmp)], "out": str(out)})
    except Exception:
        out.unlink(missing_ok=True)
        raise
    finally:
        tmp.unlink(missing_ok=True)
    name = PurePosixPath(storage.sanitize_filename(file.filename)).stem[:180]
    return _new_document(db, user, request, out, f"{name} (repaired).pdf", "Rebuilt from a damaged upload", "tools.repair")


@router.post("/extract-images")
def extract_images(body: _Doc, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    out = _tmp("zip")
    try:
        _run("extract_images", {"inputs": [str(_source(doc))], "out": str(out)})
    except Exception:
        out.unlink(missing_ok=True)
        raise
    audit.record(db, "tools.extract_images", user.id, doc.id, client_ip(request))
    db.commit()
    return _download(out, f"{_stem(doc)} (images).zip", "application/zip")


# ---------- forms ----------
@router.get("/form/{document_id}")
def form_fields(document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(document_id, db, user)
    return {"document_id": doc.id, "fields": _run("form_fields", {"inputs": [str(_source(doc))]})["fields"]}


@router.post("/form", response_model=DocumentOut)
def form_fill(body: FormFillRequest, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    src, out = _source(doc), _tmp("out")
    result = _run("form_fill", {"inputs": [str(src)], "out": str(out), "values": body.values})
    return _new_version(db, user, request, doc, out, result["page_count"], f"Filled {result['filled']} form field(s)", "tools.form_fill")
