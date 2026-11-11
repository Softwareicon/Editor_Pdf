import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.database import Base
from app.services import storage

BACKEND = Path(__file__).resolve().parents[2] / "backend"


def test_health_and_security_headers(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_errors_use_one_envelope(client):
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404 and set(r.json()) == {"error"} and r.json()["error"]["code"] == "http_404"


def test_openapi_documents_every_endpoint(client):
    paths = client.get("/api/openapi.json").json()["paths"]
    for p in ["/api/auth/register", "/api/auth/login", "/api/auth/logout", "/api/auth/me", "/api/auth/refresh",
              "/api/documents/upload", "/api/documents", "/api/documents/{document_id}",
              "/api/documents/{document_id}/download", "/api/editor/{document_id}/metadata",
              "/api/editor/{document_id}/pages", "/api/editor/{document_id}/versions",
              "/api/editor/{document_id}/undo", "/api/editor/{document_id}/redo", "/api/pages/add", "/api/pages/delete",
              "/api/pages/reorder", "/api/pages/rotate", "/api/pages/extract", "/api/pages/duplicate", "/api/pages/reverse"]:
        assert p in paths, p


def test_storage_refuses_paths_outside_the_root():
    from app.errors import ApiError
    with pytest.raises(ApiError):
        storage.resolve("../outside.pdf")
    with pytest.raises(ApiError):
        storage.resolve("uploads/../../outside.pdf")


def test_temp_cleanup_removes_only_stale_files(storage_root):
    old, new = storage_root / "temp" / "old.upload", storage_root / "temp" / "new.upload"
    old.write_bytes(b"x"); new.write_bytes(b"x")
    stale = time.time() - 48 * 3600
    os.utime(old, (stale, stale))
    assert storage.cleanup_temp() == 1
    assert not old.exists() and new.exists()
    new.unlink()


def test_production_refuses_to_start_without_a_strong_secret():
    from app.config import Settings
    with pytest.raises(ValueError):
        Settings(environment="production", secret_key="short", _env_file=None)


def test_alembic_migration_matches_the_models(tmp_path):
    """`alembic upgrade head` on an empty database must produce exactly the schema the ORM expects."""
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    env = {**os.environ, "PDFMASTER_DATABASE_URL": url, "PDFMASTER_STORAGE_DIR": str(tmp_path / "s")}
    run = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env,
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    insp = inspect(create_engine(url))
    migrated = {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names() if t != "alembic_version"}
    expected = {t.name: {c.name for c in t.columns} for t in Base.metadata.sorted_tables}
    assert migrated == expected
    assert {"users", "documents", "document_versions", "processing_jobs", "user_settings", "audit_logs"} <= set(migrated)
    check = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=BACKEND, env=env, capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr
