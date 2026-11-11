"""Read-only document structure for the viewer (Phase 1). Editing endpoints arrive in Phase 2."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas.document import MetadataOut, PagesOut
from ..security.deps import get_current_user
from ..services import storage
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
