"""Document versions: every edit writes a new file and moves the document's version pointer.

Undo/redo only move the pointer. A new edit made after an undo discards the undone versions.
"""
from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ApiError
from ..models import Document, DocumentVersion
from . import storage
from .pdf_inspect import inspect_pdf
from .sandbox import run_worker


def _point_at(doc: Document, version: DocumentVersion) -> None:
    if version.page_count is None or version.file_size is None:  # rows created before these columns existed
        path = storage.resolve(version.storage_path)
        version.file_size = path.stat().st_size
        version.page_count = inspect_pdf(path)["page_count"]
    doc.current_version = version.version_number
    doc.storage_path = version.storage_path
    doc.stored_filename = version.storage_path.rsplit("/", 1)[-1]
    doc.page_count, doc.file_size = version.page_count, version.file_size


def step(doc: Document, direction: int) -> DocumentVersion:
    """Move the pointer to the previous (-1) or next (+1) version."""
    if direction < 0:
        candidates = [v for v in doc.versions if v.version_number < doc.current_version]
        target = max(candidates, key=lambda v: v.version_number, default=None)
    else:
        candidates = [v for v in doc.versions if v.version_number > doc.current_version]
        target = min(candidates, key=lambda v: v.version_number, default=None)
    if target is None:
        raise ApiError(409, "nothing_to_undo" if direction < 0 else "nothing_to_redo",
                       "Nothing to undo" if direction < 0 else "Nothing to redo")
    if not storage.resolve(target.storage_path).is_file():
        raise ApiError(410, "file_missing", "That version's file is no longer available")
    _point_at(doc, target)
    return target


def run_arrangement(doc: Document, entries: list[dict]) -> tuple[str, int, int, dict]:
    """Build a rearranged copy of the document's current file. Returns (relative_path, size, page_count, info)."""
    s = get_settings()
    if doc.processing_status == "encrypted":
        raise ApiError(409, "document_encrypted", "Password-protected PDFs cannot be edited until they are unlocked")
    src = storage.resolve(doc.storage_path)
    if not src.is_file():
        raise ApiError(410, "file_missing", "The stored file is no longer available")
    tmp = s.temp_dir / f"{uuid.uuid4().hex}.out"
    try:
        import json
        result = run_worker("page_ops.py", [str(src), str(tmp), str(s.max_pages)], stdin=json.dumps({"pages": entries}))
        if not result.get("ok"):
            if result.get("error") == "encrypted":
                raise ApiError(409, "document_encrypted", "Encrypted PDFs cannot be edited until they are unlocked")
            if result.get("error") == "bad_request":
                raise ApiError(422, "invalid_pages", f"Invalid page selection: {result.get('detail')}")
            raise ApiError(422, "edit_failed", "This PDF could not be modified; its structure may be damaged")
        size = tmp.stat().st_size
        _, rel = storage.commit_upload(tmp, doc.user_id)
        return rel, size, result["page_count"], result
    finally:
        tmp.unlink(missing_ok=True)


def add_version(db: Session, doc: Document, rel_path: str, size: int, page_count: int, description: str) -> list[str]:
    """Record a new current version. Returns paths of files that must be deleted after commit."""
    doomed = [v for v in doc.versions if v.version_number > doc.current_version]  # the redo branch
    number = max(v.version_number for v in doc.versions) + 1
    for v in doomed:
        doc.versions.remove(v)
    version = DocumentVersion(version_number=number, storage_path=rel_path, change_description=description,
                              page_count=page_count, file_size=size)
    doc.versions.append(version)
    _point_at(doc, version)
    while len(doc.versions) > get_settings().max_versions:
        oldest = doc.versions[0]
        doc.versions.remove(oldest)
        doomed.append(oldest)
    db.flush()
    return [v.storage_path for v in doomed]


def run_overlay(doc: Document, items: list[dict]) -> tuple[str, int, int]:
    """Draw overlay items on a copy of the document's current file. Returns (relative_path, size, page_count)."""
    import json

    s = get_settings()
    if doc.processing_status == "encrypted":
        raise ApiError(409, "document_encrypted", "Password-protected PDFs cannot be edited until they are unlocked")
    src = storage.resolve(doc.storage_path)
    if not src.is_file():
        raise ApiError(410, "file_missing", "The stored file is no longer available")
    tmp = s.temp_dir / f"{uuid.uuid4().hex}.out"
    try:
        result = run_worker("overlay_ops.py", [str(src), str(tmp)], stdin=json.dumps({"items": items}))
        if not result.get("ok"):
            if result.get("error") == "encrypted":
                raise ApiError(409, "document_encrypted", "Encrypted PDFs cannot be edited until they are unlocked")
            if result.get("error") == "bad_request":
                raise ApiError(422, "invalid_edit", f"The edits could not be applied: {result.get('detail')}")
            raise ApiError(422, "edit_failed", "This PDF could not be modified; its structure may be damaged")
        size = tmp.stat().st_size
        _, rel = storage.commit_upload(tmp, doc.user_id)
        return rel, size, result["page_count"]
    finally:
        tmp.unlink(missing_ok=True)
