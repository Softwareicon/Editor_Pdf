"""Local file storage. All paths are generated server-side; user input never becomes a path."""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path

from ..config import get_settings
from ..errors import ApiError

logger = logging.getLogger("pdfmaster.storage")
PDF_MAGIC = b"%PDF-"
_CHUNK = 1024 * 1024


def sanitize_filename(name: str | None) -> str:
    """Display name only (never used on disk): strip directories and control characters."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    if not name:
        name = "document.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[-255:]


def resolve(relative_path: str) -> Path:
    """Map a stored relative path to an absolute one, refusing anything outside the storage root."""
    root = get_settings().storage_dir.resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root):
        raise ApiError(500, "storage_error", "Invalid storage path")
    return path


def save_upload_to_temp(fileobj, max_bytes: int) -> tuple[Path, int]:
    """Stream an upload to a temp file, enforcing the size limit and the PDF signature."""
    tmp = get_settings().temp_dir / f"{uuid.uuid4().hex}.upload"
    size = 0
    try:
        with open(tmp, "wb") as out:
            first = True
            while chunk := fileobj.read(_CHUNK):
                if first:
                    # The PDF spec tolerates leading junk; mainstream readers accept the header in the first 1024 bytes.
                    if PDF_MAGIC not in chunk[:1024]:
                        raise ApiError(415, "not_a_pdf", "Only PDF files can be uploaded")
                    first = False
                size += len(chunk)
                if size > max_bytes:
                    raise ApiError(413, "file_too_large",
                                   f"File exceeds the {get_settings().max_upload_mb} MB limit")
                out.write(chunk)
        if size == 0:
            raise ApiError(400, "empty_file", "The uploaded file is empty")
        return tmp, size
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def commit_upload(tmp: Path, user_id: str) -> tuple[str, str]:
    """Move a validated temp file into the user's private directory. Returns (stored_filename, relative_path)."""
    stored = f"{uuid.uuid4().hex}.pdf"
    user_dir = get_settings().uploads_dir / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, user_dir / stored)
    return stored, f"uploads/{user_id}/{stored}"


def secure_delete(relative_path: str) -> None:
    """Overwrite then unlink. Best effort: SSD wear-levelling, journaling/copy-on-write file systems
    and backups can retain old blocks; use full-disk or bucket encryption for stronger guarantees."""
    try:
        path = resolve(relative_path)
        if path.is_file():
            length = path.stat().st_size
            with open(path, "r+b") as f:
                remaining = length
                while remaining > 0:
                    n = min(_CHUNK, remaining)
                    f.write(b"\x00" * n)
                    remaining -= n
                f.flush()
                os.fsync(f.fileno())
            path.unlink()
    except OSError:
        logger.exception("Failed to delete %s", relative_path)


def cleanup_temp(max_age_hours: float | None = None) -> int:
    """Remove stale files from the temp directory."""
    s = get_settings()
    cutoff = time.time() - (s.temp_max_age_hours if max_age_hours is None else max_age_hours) * 3600
    removed = 0
    for p in s.temp_dir.iterdir():
        try:
            if p.is_file() and p.name != ".gitkeep" and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            logger.warning("Could not remove temp file %s", p)
    return removed
