"""Content editing by overlay: text, whiteout, shapes, highlights, freehand ink, images and signatures
are drawn on top of the pages. Each save produces a new document version (undo with /api/editor/{id}/undo)."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import ApiError
from ..models import User
from ..schemas.document import DocumentOut
from ..schemas.edit import ApplyEditsRequest
from ..security.deps import client_ip, get_current_user
from ..services import audit, storage, versions
from .documents import get_owned_document

router = APIRouter(prefix="/api/edit", tags=["Content Editing"])


@router.post("/apply", response_model=DocumentOut)
def apply_edits(body: ApplyEditsRequest, request: Request, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    doc = get_owned_document(body.document_id, db, user)
    if doc.page_count is None:
        raise ApiError(409, "document_encrypted", "Password-protected PDFs cannot be edited until they are unlocked")
    if any(item.page > doc.page_count for item in body.items):
        raise ApiError(422, "invalid_pages", f"The document has only {doc.page_count} pages")
    rel, size, page_count = versions.run_overlay(doc, [item.model_dump() for item in body.items])
    n = len(body.items)
    try:
        doomed = versions.add_version(db, doc, rel, size, page_count, f"Added {n} edit{'s' if n != 1 else ''}")
        audit.record(db, "edit.apply", user.id, doc.id, client_ip(request))
        db.commit()
    except Exception:
        db.rollback()
        storage.secure_delete(rel)
        raise
    for path in doomed:
        storage.secure_delete(path)
    return doc
