"""Document structure for the viewer, plus version history, undo and redo."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas.document import DocumentOut, MetadataOut, PagesOut, VersionsOut
from ..security.deps import client_ip, get_current_user
from ..services import audit, storage, versions
from ..services.pdf_inspect import inspect_pdf
from .documents import get_owned_document

router = APIRouter(prefix="/api/editor", tags=["PDF Editor"])


@router.get("/{document_id}/metadata", response_model=MetadataOut)
def get_metadata(document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(document_id, db, user)
    info = inspect_pdf(storage.resolve(doc.storage_path))
    return MetadataOut(document_id=doc.id, filename=doc.original_filename, file_size=doc.file_size,
                       pdf_version=info["pdf_version"], encrypted=info["encrypted"],
                       needs_password=info["needs_password"], page_count=info["page_count"],
                       info=info["metadata"])


@router.get("/{document_id}/pages", response_model=PagesOut)
def get_pages(document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(document_id, db, user)
    info = inspect_pdf(storage.resolve(doc.storage_path))
    return PagesOut(document_id=doc.id, page_count=info["page_count"],
                    needs_password=info["needs_password"], pages=info["pages"])


@router.get("/{document_id}/versions", response_model=VersionsOut)
def get_versions(document_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    doc = get_owned_document(document_id, db, user)
    return VersionsOut(document_id=doc.id, current_version=doc.current_version, versions=doc.versions)


def _step(document_id: str, direction: int, request: Request, db: Session, user: User):
    doc = get_owned_document(document_id, db, user)
    versions.step(doc, direction)
    audit.record(db, "document.undo" if direction < 0 else "document.redo", user.id, doc.id, client_ip(request))
    db.commit()
    return doc


@router.post("/{document_id}/undo", response_model=DocumentOut)
def undo(document_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _step(document_id, -1, request, db, user)


@router.post("/{document_id}/redo", response_model=DocumentOut)
def redo(document_id: str, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _step(document_id, +1, request, db, user)
