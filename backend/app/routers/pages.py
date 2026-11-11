"""Page management. Each call produces a new document version (undo with POST /api/editor/{id}/undo)."""
from __future__ import annotations

from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import ApiError
from ..models import Document, DocumentVersion, User
from ..schemas.document import DocumentOut
from ..schemas.pages import AddPagesRequest, DocumentRef, PagesRequest, ReorderRequest, RotateRequest
from ..security.deps import client_ip, get_current_user
from ..services import audit, storage, versions
from .documents import get_owned_document

router = APIRouter(prefix="/api/pages", tags=["Page Management"])


def _count(doc: Document) -> int:
    if doc.page_count is None:
        raise ApiError(409, "document_encrypted", "Password-protected PDFs cannot be edited until they are unlocked")
    return doc.page_count


def _check(pages: list[int], count: int) -> None:
    if any(p > count for p in pages):
        raise ApiError(422, "invalid_pages", f"The document has only {count} pages")


def _plural(n: int) -> str:
    return f"{n} page{'s' if n != 1 else ''}"


def _apply(db: Session, doc: Document, user: User, request: Request, entries: list[dict], description: str,
           action: str) -> Document:
    rel, size, page_count, _ = versions.run_arrangement(doc, entries)
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


@router.post("/delete", response_model=DocumentOut)
def delete_pages(body: PagesRequest, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    count = _count(doc)
    _check(body.pages, count)
    gone = set(body.pages)
    if len(gone) >= count:
        raise ApiError(422, "cannot_delete_all", "A document must keep at least one page")
    entries = [{"src": i} for i in range(count) if i + 1 not in gone]
    return _apply(db, doc, user, request, entries, f"Deleted {_plural(len(gone))}", "pages.delete")


@router.post("/rotate", response_model=DocumentOut)
def rotate_pages(body: RotateRequest, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    count = _count(doc)
    _check(body.pages, count)
    turn = set(body.pages)
    entries = [{"src": i, "rotate": body.angle if i + 1 in turn else 0} for i in range(count)]
    return _apply(db, doc, user, request, entries, f"Rotated {_plural(len(turn))} by {body.angle}°", "pages.rotate")


@router.post("/reorder", response_model=DocumentOut)
def reorder_pages(body: ReorderRequest, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    count = _count(doc)
    if sorted(body.order) != list(range(1, count + 1)):
        raise ApiError(422, "invalid_pages", "order must list every page of the document exactly once")
    return _apply(db, doc, user, request, [{"src": p - 1} for p in body.order], "Reordered pages", "pages.reorder")


@router.post("/reverse", response_model=DocumentOut)
def reverse_pages(body: DocumentRef, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    entries = [{"src": i} for i in reversed(range(_count(doc)))]
    return _apply(db, doc, user, request, entries, "Reversed page order", "pages.reverse")


@router.post("/duplicate", response_model=DocumentOut)
def duplicate_pages(body: PagesRequest, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    count = _count(doc)
    _check(body.pages, count)
    twice = set(body.pages)
    entries = [{"src": i} for i in range(count) for _ in range(2 if i + 1 in twice else 1)]
    return _apply(db, doc, user, request, entries, f"Duplicated {_plural(len(twice))}", "pages.duplicate")


@router.post("/add", response_model=DocumentOut)
def add_blank_pages(body: AddPagesRequest, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    count = _count(doc)
    if body.after > count:
        raise ApiError(422, "invalid_pages", f"The document has only {count} pages")
    blank = {"blank": {"w": body.width, "h": body.height} if body.width else {"like": max(body.after - 1, 0)}}
    entries = [{"src": i} for i in range(count)]
    entries[body.after:body.after] = [blank] * body.count
    return _apply(db, doc, user, request, entries, f"Added {_plural(body.count)}", "pages.add")


@router.post("/extract", response_model=DocumentOut, status_code=201)
def extract_pages(body: PagesRequest, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Copy the selected pages into a new document. The source document is not changed."""
    doc = get_owned_document(body.document_id, db, user)
    _check(body.pages, _count(doc))
    rel, size, page_count, _ = versions.run_arrangement(doc, [{"src": p - 1} for p in sorted(body.pages)])
    stem = PurePosixPath(doc.original_filename).stem[:200]
    new = Document(user_id=user.id, original_filename=f"{stem} (extract).pdf", stored_filename=rel.rsplit("/", 1)[-1],
                   file_size=size, page_count=page_count, storage_path=rel)
    new.versions.append(DocumentVersion(version_number=1, storage_path=rel, page_count=page_count, file_size=size,
                                        change_description=f"Extracted from {doc.original_filename}"[:500]))
    db.add(new)
    try:
        db.flush()
        audit.record(db, "pages.extract", user.id, new.id, client_ip(request))
        db.commit()
    except Exception:
        db.rollback()
        storage.secure_delete(rel)
        raise
    return new
