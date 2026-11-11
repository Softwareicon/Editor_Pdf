"""Structural inspection of a PDF (page count, sizes, metadata) in a sandboxed worker."""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import get_settings
from ..errors import ApiError
from .sandbox import run_worker

logger = logging.getLogger("pdfmaster.inspect")


def inspect_pdf(path: Path) -> dict:
    """Return structural info for a PDF, or raise ApiError(422) if it cannot be parsed safely."""
    s = get_settings()
    result = run_worker("inspect_pdf.py", [str(path), str(s.max_pages)])
    if not result.get("ok"):
        if result.get("error") == "too_many_pages":
            raise ApiError(422, "too_many_pages", f"The PDF has too many pages (limit {s.max_pages})")
        logger.info("Rejected PDF: %s", result.get("detail"))
        raise ApiError(422, "invalid_pdf", "The file is corrupted or is not a valid PDF")
    return result
