import base64
import io

from PIL import Image
from pypdf import PdfReader, PdfWriter

from conftest import register, upload
from pdf_factory import build_encrypted_pdf, build_pdf


def make(client, auth, n=2, **kw):
    return upload(client, auth, build_pdf([f"P{i}" for i in range(1, n + 1)], **kw)).json()["id"]


def reader(client, auth, doc_id) -> PdfReader:
    return PdfReader(io.BytesIO(client.get(f"/api/documents/{doc_id}/download", headers=auth).content))


def apply(client, auth, doc_id, *items):
    return client.post("/api/edit/apply", headers=auth, json={"document_id": doc_id, "items": list(items)})


def png(color=(255, 0, 0, 128), size=(40, 20)) -> str:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def text_positions(page):
    found = {}

    def visit(text, cm, tm, font, size):
        if text.strip():
            found[text.strip()] = (round(tm[4] * cm[0] + tm[5] * cm[2] + cm[4], 1), round(tm[4] * cm[1] + tm[5] * cm[3] + cm[5], 1))

    page.extract_text(visitor_text=visit)
    return found


def test_text_is_added_as_a_new_version_and_can_be_undone(client, auth):
    d = make(client, auth)
    r = apply(client, auth, d, {"type": "text", "page": 2, "x": 100, "y": 200, "text": "Hello edit\nSecond line", "size": 20,
                                "color": "#0033cc", "font": "times", "bold": True})
    assert r.status_code == 200, r.text
    assert r.json()["current_version"] == 2 and r.json()["can_undo"] is True and r.json()["page_count"] == 2
    pages = reader(client, auth, d).pages
    assert "Hello edit" not in pages[0].extract_text()
    assert "P2" in pages[1].extract_text() and "Hello edit" in pages[1].extract_text() and "Second line" in pages[1].extract_text()
    x, y = text_positions(pages[1])["Hello edit"]
    assert abs(x - 100) < 0.5 and abs(y - (792 - 200 - 0.9 * 20)) < 0.5   # baseline of the first line
    assert client.post(f"/api/editor/{d}/undo", headers=auth).status_code == 200
    assert "Hello edit" not in reader(client, auth, d).pages[1].extract_text()
    assert client.post(f"/api/editor/{d}/redo", headers=auth).status_code == 200
    assert "Hello edit" in reader(client, auth, d).pages[1].extract_text()


def test_all_item_types_in_one_save(client, auth):
    d = make(client, auth, 1)
    r = apply(client, auth, d,
              {"type": "rect", "page": 1, "x": 60, "y": 70, "w": 200, "h": 40, "fill": "#ffffff"},
              {"type": "rect", "page": 1, "x": 60, "y": 150, "w": 200, "h": 20, "fill": "#ffe600", "opacity": 0.4},
              {"type": "ellipse", "page": 1, "x": 300, "y": 300, "w": 80, "h": 50, "stroke": "#ff0000", "stroke_width": 2},
              {"type": "line", "page": 1, "x1": 50, "y1": 400, "x2": 250, "y2": 450, "color": "#00aa00", "width": 3, "arrow": True},
              {"type": "ink", "page": 1, "points": [[100, 500], [120, 520], [140, 505]], "color": "#000000", "width": 2},
              {"type": "image", "page": 1, "x": 300, "y": 500, "w": 80, "h": 40, "data": "data:image/png;base64," + png()},
              {"type": "text", "page": 1, "x": 72, "y": 80, "text": "Replaced"})
    assert r.status_code == 200, r.text
    page = reader(client, auth, d).pages[0]
    assert "Replaced" in page.extract_text() and "P1" in page.extract_text()  # whiteout hides, it does not remove
    assert len(page.images) == 1
    versions = client.get(f"/api/editor/{d}/versions", headers=auth).json()["versions"]
    assert versions[-1]["change_description"] == "Added 7 edits"


def test_placement_follows_page_rotation_and_cropbox(client, auth):
    # A 600x800 page whose visible area is offset and which is shown rotated by 90 degrees clockwise.
    src = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf(["Rot"], size=(600, 800)))))
    src.pages[0].cropbox.lower_left = (50, 100)
    src.pages[0].cropbox.upper_right = (550, 700)
    src.pages[0].rotate(90)
    buf = io.BytesIO()
    src.write(buf)
    d = upload(client, auth, buf.getvalue()).json()["id"]
    size = client.get(f"/api/editor/{d}/pages", headers=auth).json()["pages"][0]
    assert (size["width"], size["height"], size["rotation"]) == (500, 600, 90)   # displayed 600 wide, 500 high
    assert apply(client, auth, d, {"type": "text", "page": 1, "x": 30, "y": 40, "text": "Mark", "size": 10}).status_code == 200
    page = reader(client, auth, d).pages[0]
    assert page.rotation == 90
    x, y = text_positions(page)["Mark"]
    # display (u right, v up from the bottom-left of the 600x500 view) -> user space: x = x1 - v, y = y0 + u
    v = 500 - (40 + 9)
    assert abs(x - (550 - v)) < 0.5 and abs(y - (100 + 30)) < 0.5


def test_validation_and_limits(client, auth):
    d = make(client, auth)
    text = {"type": "text", "page": 1, "x": 10, "y": 10, "text": "ok"}
    assert apply(client, auth, d, {**text, "page": 3}).json()["error"]["code"] == "invalid_pages"
    assert apply(client, auth, d, {**text, "page": 0}).status_code == 422
    assert apply(client, auth, d, {**text, "color": "red"}).status_code == 422
    assert apply(client, auth, d, {**text, "text": "नमस्ते"}).status_code == 422   # sent as an image by the editor
    assert apply(client, auth, d, {**text, "type": "stamp"}).status_code == 422
    assert apply(client, auth, d, {"type": "rect", "page": 1, "x": 1, "y": 1, "w": 5, "h": 5}).status_code == 422   # invisible
    assert apply(client, auth, d).status_code == 422
    bad = apply(client, auth, d, {"type": "image", "page": 1, "x": 1, "y": 1, "w": 5, "h": 5,
                                  "data": base64.b64encode(b"this is not an image at all").decode()})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_edit"
    assert client.get(f"/api/documents/{d}", headers=auth).json()["current_version"] == 1   # nothing was saved


def test_other_users_and_encrypted_documents_are_refused(client, auth):
    d = make(client, auth)
    text = {"type": "text", "page": 1, "x": 10, "y": 10, "text": "nope"}
    mallory = register(client, "mallory@example.com", "Mallory")
    assert apply(client, mallory, d, text).status_code == 404
    assert client.post("/api/edit/apply", json={"document_id": d, "items": [text]}).status_code == 401
    locked = upload(client, auth, build_encrypted_pdf("test1234")).json()["id"]
    assert apply(client, auth, locked, text).json()["error"]["code"] == "document_encrypted"
