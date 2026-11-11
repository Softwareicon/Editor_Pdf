from app.config import get_settings
from app.database import SessionLocal
from app.models import AuditLog, Document, DocumentVersion
from conftest import register, upload
from pdf_factory import build_encrypted_pdf, build_pdf


def test_upload_list_get_download_roundtrip(client, auth, pdf_bytes, storage_root):
    r = upload(client, auth, pdf_bytes, "Quarterly Report.pdf")
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["original_filename"] == "Quarterly Report.pdf" and doc["page_count"] == 3
    assert doc["file_size"] == len(pdf_bytes) and doc["processing_status"] == "ready"
    assert "storage_path" not in doc and "stored_filename" not in doc  # internal paths are never exposed

    listing = client.get("/api/documents", headers=auth).json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == doc["id"]
    assert client.get(f"/api/documents/{doc['id']}", headers=auth).json()["id"] == doc["id"]

    dl = client.get(f"/api/documents/{doc['id']}/download", headers=auth)
    assert dl.status_code == 200 and dl.content == pdf_bytes
    assert dl.headers["content-type"] == "application/pdf"
    assert 'filename="Quarterly Report.pdf"' in dl.headers["content-disposition"]

    with SessionLocal() as db:
        row = db.query(Document).one()
        assert (storage_root / row.storage_path).read_bytes() == pdf_bytes
        assert db.query(DocumentVersion).one().version_number == 1
        actions = {a.action for a in db.query(AuditLog).all()}
        assert {"document.upload", "document.download"} <= actions
    assert not any(p.suffix == ".upload" for p in (storage_root / "temp").iterdir())  # temp cleaned up


def test_delete_removes_row_file_and_versions(client, auth, pdf_bytes, storage_root):
    doc = upload(client, auth, pdf_bytes).json()
    with SessionLocal() as db:
        path = storage_root / db.query(Document).one().storage_path
    assert path.exists()
    assert client.delete(f"/api/documents/{doc['id']}", headers=auth).status_code == 204
    assert not path.exists()
    assert client.get(f"/api/documents/{doc['id']}", headers=auth).status_code == 404
    with SessionLocal() as db:
        assert db.query(Document).count() == 0 and db.query(DocumentVersion).count() == 0
        assert db.query(AuditLog).filter(AuditLog.action == "document.delete").count() == 1


def test_rejects_non_pdf_corrupted_empty_and_oversized_files(client, auth, pdf_bytes, monkeypatch, storage_root):
    assert upload(client, auth, b"MZ\x90\x00 this is an exe renamed", "evil.pdf").json()["error"]["code"] == "not_a_pdf"
    assert upload(client, auth, b"<html>%PDF</html>" * 10, "page.pdf").status_code == 415
    corrupted = upload(client, auth, b"%PDF-1.7\n" + b"garbage " * 200, "broken.pdf")
    assert corrupted.status_code == 422 and corrupted.json()["error"]["code"] == "invalid_pdf"
    truncated = upload(client, auth, pdf_bytes[: len(pdf_bytes) // 3], "truncated.pdf")
    assert truncated.status_code == 422
    assert upload(client, auth, b"", "empty.pdf").status_code == 400

    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    big = build_pdf(["x"]) + b"\n%" + b"0" * (2 * 1024 * 1024)
    assert upload(client, auth, big, "big.pdf").status_code == 413

    assert client.get("/api/documents", headers=auth).json()["total"] == 0
    assert [p.name for p in (storage_root / "temp").iterdir() if p.name != ".gitkeep"] == []


def test_page_limit_is_enforced(client, auth, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_pages", 2)
    r = upload(client, auth, build_pdf(["1", "2", "3"]))
    assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_pages"


def test_malicious_filenames_never_reach_the_filesystem(client, auth, pdf_bytes, storage_root):
    r = upload(client, auth, pdf_bytes, "../../../etc/passwd")
    assert r.status_code == 201 and r.json()["original_filename"] == "passwd.pdf"
    r = upload(client, auth, pdf_bytes, "..\\..\\windows\\system32\\evil.pdf")
    assert r.json()["original_filename"] == "evil.pdf"
    from app.services.storage import sanitize_filename
    assert sanitize_filename("a\x00b\r\n.pdf") == "ab.pdf" and sanitize_filename("") == "document.pdf"
    user_id = client.get("/api/auth/me", headers=auth).json()["id"]
    files = list((storage_root / "uploads" / user_id).iterdir())
    assert len(files) == 2
    assert all(p.parent.parent == storage_root / "uploads" for p in storage_root.rglob("*.pdf"))
    assert all(len(p.stem) == 32 for p in files)  # server-generated names only


def test_users_cannot_see_or_touch_each_others_documents(client, pdf_bytes):
    alice = register(client, "alice@example.com")
    bob = register(client, "bob@example.com", "Bob")
    doc_id = upload(client, alice, pdf_bytes).json()["id"]

    assert client.get("/api/documents", headers=bob).json()["total"] == 0
    for method, path in [("get", f"/api/documents/{doc_id}"), ("get", f"/api/documents/{doc_id}/download"),
                         ("delete", f"/api/documents/{doc_id}"), ("get", f"/api/editor/{doc_id}/metadata"),
                         ("get", f"/api/editor/{doc_id}/pages")]:
        r = getattr(client, method)(path, headers=bob)
        assert r.status_code == 404, (method, path)
        assert r.json()["error"]["code"] == "document_not_found"
    assert client.get(f"/api/documents/{doc_id}", headers=alice).status_code == 200  # still there


def test_all_document_endpoints_require_authentication(client):
    for method, path in [("get", "/api/documents"), ("get", "/api/documents/x"), ("get", "/api/documents/x/download"),
                         ("delete", "/api/documents/x"), ("get", "/api/editor/x/metadata"), ("get", "/api/editor/x/pages")]:
        assert getattr(client, method)(path).status_code == 401, path
    assert client.post("/api/documents/upload", files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")}).status_code == 401


def test_editor_metadata_and_pages(client, auth):
    doc_id = upload(client, auth, build_pdf(["a", "b"], size=(792, 612), title="My (Title)")).json()["id"]
    meta = client.get(f"/api/editor/{doc_id}/metadata", headers=auth).json()
    assert meta["pdf_version"] == "1.4" and meta["page_count"] == 2 and meta["encrypted"] is False
    assert meta["info"]["title"] == "My (Title)"
    pages = client.get(f"/api/editor/{doc_id}/pages", headers=auth).json()
    assert pages["pages"] == [{"index": 1, "width": 792.0, "height": 612.0, "rotation": 0},
                              {"index": 2, "width": 792.0, "height": 612.0, "rotation": 0}]


def test_password_protected_pdf_is_stored_but_never_opened_without_the_password(client, auth):
    r = upload(client, auth, build_encrypted_pdf("s3cret-pw"), "locked.pdf")
    assert r.status_code == 201
    assert r.json()["processing_status"] == "encrypted" and r.json()["page_count"] is None
    meta = client.get(f"/api/editor/{r.json()['id']}/metadata", headers=auth).json()
    assert meta["encrypted"] is True and meta["needs_password"] is True and meta["info"] == {}


def test_pagination(client, auth, pdf_bytes):
    for i in range(3):
        upload(client, auth, pdf_bytes, f"doc{i}.pdf")
    page = client.get("/api/documents?limit=2&offset=2", headers=auth).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    assert client.get("/api/documents?limit=0", headers=auth).status_code == 422
