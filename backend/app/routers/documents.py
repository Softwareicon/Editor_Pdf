"""Document upload, listing, download and deletion. Every query is scoped to the current user."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..errors import ApiError
from ..models import Document, DocumentVersion, User
from ..schemas.document import BlankDocumentRequest, DocumentList, DocumentOut
from ..security.deps import client_ip, get_current_user
from ..services import audit, storage
from ..services.pdf_inspect import inspect_pdf

router = APIRouter(prefix="/api/documents", tags=["Documents"])


def get_owned_document(document_id: str, db: Session, user: User) -> Document:
    """Fetch a document only if it belongs to the user. Others' documents look identical to missing ones."""
    doc = db.query(Document).filter(Document.id == document_id, Document.user_id == user.id).one_or_none()
    if doc is None:
        raise ApiError(404, "document_not_found", "Document not found")
    return doc


PAGE_SIZES = {"a4": (595.28, 841.89), "letter": (612, 792), "legal": (612, 1008)}


@router.post("/blank", response_model=DocumentOut, status_code=201)
def create_blank_document(body: BlankDocumentRequest, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    """Start a new document with empty pages (then add text, images and shapes in the editor)."""
    import uuid

    from pypdf import PdfWriter

    s = get_settings()
    width, height = PAGE_SIZES[body.size]
    if body.landscape:
        width, height = height, width
    writer = PdfWriter()
    for _ in range(body.pages):
        writer.add_blank_page(width=width, height=height)
    tmp = s.temp_dir / f"{uuid.uuid4().hex}.blank"
    try:
        with open(tmp, "wb") as f:
            writer.write(f)
        size = tmp.stat().st_size
        stored, rel_path = storage.commit_upload(tmp, user.id)
    finally:
        tmp.unlink(missing_ok=True)
    doc = Document(user_id=user.id, original_filename=storage.sanitize_filename(body.name), stored_filename=stored,
                   file_size=size, page_count=body.pages, mime_type="application/pdf", storage_path=rel_path,
                   processing_status="ready")
    doc.versions.append(DocumentVersion(version_number=1, storage_path=rel_path, change_description="Blank document",
                                        page_count=body.pages, file_size=size))
    db.add(doc)
    try:
        db.flush()
        audit.record(db, "document.create_blank", user.id, doc.id, client_ip(request))
        db.commit()
    except Exception:
        db.rollback()
        storage.secure_delete(rel_path)
        raise
    return doc


@router.post("/upload", response_model=DocumentOut, status_code=201)
def upload_document(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    s = get_settings()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > s.max_upload_bytes + 1024 * 1024:
        raise ApiError(413, "file_too_large", f"File exceeds the {s.max_upload_mb} MB limit")

    tmp, size = storage.save_upload_to_temp(file.file, s.max_upload_bytes)
    try:
        info = inspect_pdf(tmp)
        stored, rel_path = storage.commit_upload(tmp, user.id)
    finally:
        tmp.unlink(missing_ok=True)

    doc = Document(user_id=user.id, original_filename=storage.sanitize_filename(file.filename),
                   stored_filename=stored, file_size=size, page_count=info["page_count"],
                   mime_type="application/pdf", storage_path=rel_path,
                   processing_status="encrypted" if info["needs_password"] else "ready")
    doc.versions.append(DocumentVersion(version_number=1, storage_path=rel_path, change_description="Original upload",
                                        page_count=info["page_count"], file_size=size))
    db.add(doc)
    try:
        db.flush()
        audit.record(db, "document.upload", user.id, doc.id, client_ip(request))
        db.commit()
    except Exception:
        db.rollback()
        storage.secure_delete(rel_path)
        raise
    return doc


@router.get("", response_model=DocumentList)
def list_documents(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(Document).filter(Document.user_id == user.id)
    items = q.order_by(Document.created_at.desc(), Document.id).offset(offset).limit(limit).all()
    return DocumentList(items=items, total=q.count(), limit=limit, offset=offset)


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return get_owned_document(document_id, db, user)


@router.get("/{document_id}/download")
def download_document(document_id: str, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    doc = get_owned_document(document_id, db, user)
    path = storage.resolve(doc.storage_path)
    if not path.is_file():
        raise ApiError(410, "file_missing", "The stored file is no longer available")
    audit.record(db, "document.download", user.id, doc.id, client_ip(request))
    db.commit()
    ascii_name = doc.original_filename.encode("ascii", "ignore").decode().replace('"', "") or "document.pdf"
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(doc.original_filename)}"
    return FileResponse(path, media_type="application/pdf", headers={"Content-Disposition": disposition})


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    doc = get_owned_document(document_id, db, user)
    paths = {doc.storage_path, *(v.storage_path for v in doc.versions)}
    audit.record(db, "document.delete", user.id, doc.id, client_ip(request))
    db.delete(doc)
    db.commit()
    for p in paths:
        storage.secure_delete(p)
