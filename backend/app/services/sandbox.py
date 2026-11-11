"""Runs PDF worker scripts in isolated, resource-limited subprocesses.

A malicious or malformed PDF can at worst crash or exhaust the short-lived worker, never the API.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from ..config import get_settings
from ..errors import ApiError

logger = logging.getLogger("pdfmaster.sandbox")
WORKERS = Path(__file__).resolve().parents[1] / "workers"


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


def _env() -> dict:
    # Only what Python needs to start and find its packages; no application secrets reach the worker.
    keep = ("SYSTEMROOT", "PYTHONPATH", "VIRTUAL_ENV", "HOME", "USERPROFILE", "APPDATA")
    return {"PYTHONIOENCODING": "utf-8", "PATH": "", **{k: os.environ[k] for k in keep if k in os.environ}}


def run_worker(script: str, args: list[str], stdin: str | None = None) -> dict:
    """Run workers/<script> and return the JSON object it prints as its last stdout line."""
    s = get_settings()
    try:
        proc = subprocess.run(
            [sys.executable, str(WORKERS / script), *args], input=stdin, capture_output=True, text=True,
            timeout=s.inspect_timeout_seconds, env=_env(),
            preexec_fn=_limits(s.inspect_memory_limit_mb, s.inspect_timeout_seconds),
        )
    except subprocess.TimeoutExpired:
        raise ApiError(422, "pdf_too_complex", "The PDF took too long to process and was rejected")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        logger.warning("Worker %s died (rc=%s): %s", script, proc.returncode, proc.stderr[-300:])
        raise ApiError(422, "invalid_pdf", "The file could not be processed as a PDF")
