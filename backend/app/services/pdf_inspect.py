"""Runs the PDF inspection worker in an isolated subprocess with time/memory limits."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from ..config import get_settings
from ..errors import ApiError

logger = logging.getLogger("pdfmaster.inspect")
_WORKER = Path(__file__).resolve().parents[1] / "workers" / "inspect_pdf.py"


def _limits(memory_mb: int, cpu_seconds: int):
    try:
        import resource  # POSIX only; on Windows the wall-clock timeout still applies
    except ImportError:
        return None

    def _apply():
        mem = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return _apply


def inspect_pdf(path: Path) -> dict:
    """Return structural info for a PDF, or raise ApiError(422) if it cannot be parsed safely."""
    s = get_settings()
    try:
        proc = subprocess.run(
            [sys.executable, str(_WORKER), str(path), str(s.max_pages)],
            capture_output=True, text=True, timeout=s.inspect_timeout_seconds,
            preexec_fn=_limits(s.inspect_memory_limit_mb, s.inspect_timeout_seconds),
            env={"PYTHONIOENCODING": "utf-8", "PATH": "", **_passthrough_env()},
        )
    except subprocess.TimeoutExpired:
        raise ApiError(422, "pdf_too_complex", "The PDF took too long to analyse and was rejected")
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        logger.warning("Inspection worker died (rc=%s): %s", proc.returncode, proc.stderr[-300:])
        raise ApiError(422, "invalid_pdf", "The file could not be read as a PDF")
    if not result.get("ok"):
        if result.get("error") == "too_many_pages":
            raise ApiError(422, "too_many_pages", f"The PDF has too many pages (limit {s.max_pages})")
        logger.info("Rejected PDF: %s", result.get("detail"))
        raise ApiError(422, "invalid_pdf", "The file is corrupted or is not a valid PDF")
    return result


def _passthrough_env() -> dict:
    import os

    # Only what Python needs to start and find its packages; no application secrets reach the worker.
    return {k: os.environ[k] for k in ("SYSTEMROOT", "PYTHONPATH", "VIRTUAL_ENV", "HOME", "USERPROFILE", "APPDATA") if k in os.environ}
