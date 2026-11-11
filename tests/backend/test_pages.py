import io

from pypdf import PdfReader, PdfWriter

from app.config import get_settings
from app.database import SessionLocal
from app.models import Document, DocumentVersion
from conftest import register, upload
from pdf_factory import build_encrypted_pdf, build_pdf


def make(client, auth, n=5, **kw):
    return upload(client, auth, build_pdf([f"P{i}" for i in range(1, n + 1)], **kw)).json()["id"]


def reader(client, auth, doc_id) -> PdfReader:
    return PdfReader(io.BytesIO(client.get(f"/api/documents/{doc_id}/download", headers=auth).content))


def texts(client, auth, doc_id):
    return [p.extract_text().strip() for p in reader(client, auth, doc_id).pages]


def post(client, auth, op, **body):
    return client.post(f"/api/pages/{op}", headers=auth, json=body)


def test_delete_pages(client, auth):
    d = make(client, auth)
    r = post(client, auth, "delete", document_id=d, pages=[2, 4])
    assert r.status_code == 200, r.text
    assert r.json()["page_count"] == 3 and r.json()["current_version"] == 2 and r.json()["can_undo"] is True
    assert texts(client, auth, d) == ["P1", "P3", "P5"]
    assert post(client, auth, "delete", document_id=d, pages=[1, 2, 3]).json()["error"]["code"] == "cannot_delete_all"
    assert post(client, auth, "delete", document_id=d, pages=[9]).status_code == 422
    assert post(client, auth, "delete", document_id=d, pages=[1, 1]).status_code == 422
    assert post(client, auth, "delete", document_id=d, pages=[0]).status_code == 422


def test_rotate_pages_keeps_content_and_is_cumulative(client, auth):
    d = make(client, auth, 3)
    assert post(client, auth, "rotate", document_id=d, pages=[2], angle=90).status_code == 200
    assert post(client, auth, "rotate", document_id=d, pages=[2, 3], angle=180).status_code == 200
    assert [p.rotation for p in reader(client, auth, d).pages] == [0, 270, 180]
    assert texts(client, auth, d) == ["P1", "P2", "P3"]
    pages = client.get(f"/api/editor/{d}/pages", headers=auth).json()["pages"]
    assert [p["rotation"] for p in pages] == [0, 270, 180]
    assert post(client, auth, "rotate", document_id=d, pages=[1], angle=45).status_code == 422


def test_reorder_and_reverse(client, auth):
    d = make(client, auth, 4)
    assert post(client, auth, "reorder", document_id=d, order=[3, 1, 4, 2]).status_code == 200
    assert texts(client, auth, d) == ["P3", "P1", "P4", "P2"]
    assert post(client, auth, "reverse", document_id=d).status_code == 200
    assert texts(client, auth, d) == ["P2", "P4", "P1", "P3"]
    for bad in ([1, 2, 3], [1, 2, 3, 3], [1, 2, 3, 5]):
        assert post(client, auth, "reorder", document_id=d, order=bad).status_code == 422


def test_duplicate_and_add_blank_pages(client, auth):
    d = make(client, auth, 2, size=(792, 612))
    assert post(client, auth, "duplicate", document_id=d, pages=[1]).json()["page_count"] == 3
    assert texts(client, auth, d) == ["P1", "P1", "P2"]
    assert post(client, auth, "add", document_id=d, after=0).json()["page_count"] == 4
    assert post(client, auth, "add", document_id=d, after=4, count=2, width=300, height=400).json()["page_count"] == 6
    pdf = reader(client, auth, d)
    assert [p.extract_text().strip() for p in pdf.pages] == ["", "P1", "P1", "P2", "", ""]
    sizes = [(float(p.mediabox.width), float(p.mediabox.height)) for p in pdf.pages]
    assert sizes[0] == (792, 612) and sizes[4] == sizes[5] == (300, 400)  # default blank size copies its neighbour
    assert post(client, auth, "add", document_id=d, after=99).status_code == 422
    assert post(client, auth, "add", document_id=d, after=1, width=300).status_code == 422


def test_extract_creates_a_new_document_and_leaves_the_source_alone(client, auth):
    d = make(client, auth)
    r = post(client, auth, "extract", document_id=d, pages=[5, 2])
    assert r.status_code == 201 and r.json()["id"] != d and r.json()["page_count"] == 2
    assert r.json()["original_filename"] == "report (extract).pdf"
    assert texts(client, auth, r.json()["id"]) == ["P2", "P5"]
    assert texts(client, auth, d) == ["P1", "P2", "P3", "P4", "P5"]
    assert client.get(f"/api/documents/{d}", headers=auth).json()["current_version"] == 1


def test_undo_redo_and_redo_branch_is_discarded_with_its_files(client, auth, storage_root):
    d = make(client, auth, 4)
    post(client, auth, "delete", document_id=d, pages=[1])
    post(client, auth, "delete", document_id=d, pages=[1])
    assert texts(client, auth, d) == ["P3", "P4"]
    u = client.post(f"/api/editor/{d}/undo", headers=auth).json()
    assert u["page_count"] == 3 and u["can_undo"] and u["can_redo"] and texts(client, auth, d) == ["P2", "P3", "P4"]
    client.post(f"/api/editor/{d}/undo", headers=auth)
    assert texts(client, auth, d) == ["P1", "P2", "P3", "P4"]
    assert client.post(f"/api/editor/{d}/undo", headers=auth).json()["error"]["code"] == "nothing_to_undo"
    r = client.post(f"/api/editor/{d}/redo", headers=auth).json()
    assert r["current_version"] == 2 and texts(client, auth, d) == ["P2", "P3", "P4"]

    with SessionLocal() as db:
        v3 = storage_root / db.query(DocumentVersion).filter_by(version_number=3).one().storage_path
    assert v3.exists()
    post(client, auth, "reverse", document_id=d)  # a new edit after undo drops the undone version 3
    assert not v3.exists()
    assert client.post(f"/api/editor/{d}/redo", headers=auth).json()["error"]["code"] == "nothing_to_redo"
    history = client.get(f"/api/editor/{d}/versions", headers=auth).json()
    assert history["current_version"] == 4
    assert [(v["version_number"], v["change_description"], v["page_count"]) for v in history["versions"]] == [
        (1, "Original upload", 4), (2, "Deleted 1 page", 3), (4, "Reversed page order", 3)]
    assert texts(client, auth, d) == ["P4", "P3", "P2"]

    # deleting the document removes every version's file
    with SessionLocal() as db:
        paths = [storage_root / v.storage_path for v in db.query(DocumentVersion).all()]
    assert len(paths) == 3 and all(p.exists() for p in paths)
    client.delete(f"/api/documents/{d}", headers=auth)
    assert not any(p.exists() for p in paths)


def test_version_history_is_capped(client, auth, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_versions", 3)
    d = make(client, auth, 2)
    for _ in range(4):
        post(client, auth, "reverse", document_id=d)
    history = client.get(f"/api/editor/{d}/versions", headers=auth).json()
    assert [v["version_number"] for v in history["versions"]] == [3, 4, 5]
    user_dir = get_settings().uploads_dir / client.get("/api/auth/me", headers=auth).json()["id"]
    assert len(list(user_dir.iterdir())) == 3


def test_metadata_and_bookmarks_survive_page_edits(client, auth):
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf(["P1", "P2", "P3"], title="Kept Title"))))
    w.add_outline_item("Chapter one", 0)
    w.add_outline_item("Chapter three", 2)
    buf = io.BytesIO()
    w.write(buf)
    d = upload(client, auth, buf.getvalue()).json()["id"]

    post(client, auth, "rotate", document_id=d, pages=[1], angle=90)
    pdf = reader(client, auth, d)
    assert pdf.metadata.title == "Kept Title" and [o.title for o in pdf.outline] == ["Chapter one", "Chapter three"]

    post(client, auth, "delete", document_id=d, pages=[2])
    pdf = reader(client, auth, d)
    assert pdf.metadata.title == "Kept Title"
    assert [(o.title, pdf.get_destination_page_number(o)) for o in pdf.outline] == [("Chapter one", 0), ("Chapter three", 1)]


def test_encrypted_documents_cannot_be_edited(client, auth):
    d = upload(client, auth, build_encrypted_pdf("pw-123456")).json()["id"]
    for op, body in [("delete", {"pages": [1]}), ("reverse", {}), ("extract", {"pages": [1]})]:
        r = post(client, auth, op, document_id=d, **body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "document_encrypted"
    # owner-password-only PDFs open without a password but must not be silently decrypted either
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf(["a", "b"]))))
    w.encrypt(user_password="", owner_password="owner-secret", algorithm="AES-256")
    buf = io.BytesIO()
    w.write(buf)
    d2 = upload(client, auth, buf.getvalue()).json()["id"]
    r = post(client, auth, "reverse", document_id=d2)
    assert r.status_code == 409 and r.json()["error"]["code"] == "document_encrypted"
    assert client.get(f"/api/documents/{d2}", headers=auth).json()["current_version"] == 1


def test_page_endpoints_are_owner_only_and_need_auth(client):
    alice, bob = register(client, "alice@example.com"), register(client, "bob@example.com", "Bob")
    d = make(client, alice, 3)
    calls = [("delete", {"pages": [1]}), ("rotate", {"pages": [1], "angle": 90}), ("reorder", {"order": [3, 2, 1]}),
             ("reverse", {}), ("duplicate", {"pages": [1]}), ("add", {"after": 1}), ("extract", {"pages": [1]})]
    for op, body in calls:
        assert post(client, bob, op, document_id=d, **body).status_code == 404, op
        assert client.post(f"/api/pages/{op}", json={"document_id": d, **body}).status_code == 401, op
    for path in ("undo", "redo"):
        assert client.post(f"/api/editor/{d}/{path}", headers=bob).status_code == 404
    assert client.get(f"/api/editor/{d}/versions", headers=bob).status_code == 404
    with SessionLocal() as db:
        assert db.query(Document).count() == 1 and db.query(DocumentVersion).count() == 1
