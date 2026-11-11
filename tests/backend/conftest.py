import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="pdfmaster-test-"))
os.environ.update({
    "PDFMASTER_ENVIRONMENT": "test",
    "PDFMASTER_SECRET_KEY": "test-secret-key-that-is-long-enough-0123456789",
    "PDFMASTER_DATABASE_URL": f"sqlite:///{(_TMP / 'test.db').as_posix()}",
    "PDFMASTER_STORAGE_DIR": str(_TMP / "storage"),
    "PDFMASTER_AUTH_RATE_LIMIT_PER_MINUTE": "1000",
    "PDFMASTER_SERVE_FRONTEND": "false",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.security.rate_limit import auth_limiter  # noqa: E402
from pdf_factory import build_pdf  # noqa: E402

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def _clean_state():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    auth_limiter.reset()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def storage_root() -> Path:
    return _TMP / "storage"


def register(client, email="alice@example.com", name="Alice"):
    r = client.post("/api/auth/register", json={"full_name": name, "email": email, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def auth(client):
    return register(client)


@pytest.fixture
def pdf_bytes() -> bytes:
    return build_pdf(["Page one", "Page two", "Page three"])


def upload(client, headers, data: bytes, name="report.pdf"):
    return client.post("/api/documents/upload", headers=headers, files={"file": (name, data, "application/pdf")})
