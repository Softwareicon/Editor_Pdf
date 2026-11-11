import io
import zipfile

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject

from conftest import register, upload
from pdf_factory import build_encrypted_pdf, build_pdf


def make(client, auth, texts, name="doc.pdf"):
    return upload(client, auth, build_pdf(texts), name).json()["id"]


def pdf(client, auth, doc_id, password=None) -> PdfReader:
    reader = PdfReader(io.BytesIO(client.get(f"/api/documents/{doc_id}/download", headers=auth).content))
    if password:
        assert reader.decrypt(password)
    return reader


def texts(client, auth, doc_id):
    return [p.extract_text().strip() for p in pdf(client, auth, doc_id).pages]


def tool(client, auth, tool_name, **body):
    return client.post(f"/api/tools/{tool_name}", headers=auth, json=body)


def test_merge_and_split(client, auth):
    a, b = make(client, auth, ["A1", "A2"]), make(client, auth, ["B1", "B2", "B3"])
    r = tool(client, auth, "merge", document_ids=[b, a], name="Both")
    assert r.status_code == 201, r.text
    merged = r.json()
    assert merged["original_filename"] == "Both.pdf" and merged["page_count"] == 5
    assert texts(client, auth, merged["id"]) == ["B1", "B2", "B3", "A1", "A2"]
    assert texts(client, auth, a) == ["A1", "A2"]                       # sources are untouched
    assert tool(client, auth, "merge", document_ids=[a]).status_code == 422
    other = register(client, "eve@example.com", "Eve")
    assert tool(client, other, "merge", document_ids=[a, b]).status_code == 404

    r = tool(client, auth, "split", document_id=merged["id"], mode="ranges", ranges="1-2, 5, 3-4")
    assert r.status_code == 201, r.text
    parts = r.json()["documents"]
    assert [p["original_filename"] for p in parts] == ["Both (pages 1-2).pdf", "Both (pages 5).pdf", "Both (pages 3-4).pdf"]
    assert [texts(client, auth, p["id"]) for p in parts] == [["B1", "B2"], ["A2"], ["B3", "A1"]]
    every = tool(client, auth, "split", document_id=merged["id"], mode="every", every=2).json()["documents"]
    assert [p["page_count"] for p in every] == [2, 2, 1]
    assert texts(client, auth, every[1]["id"]) == ["B3", "A1"]
    assert tool(client, auth, "split", document_id=merged["id"], ranges="4-9").status_code == 422
    assert tool(client, auth, "split", document_id=merged["id"], ranges="x").status_code == 422


def test_protect_and_unlock_need_the_right_password(client, auth):
    d = make(client, auth, ["Secret"], "plan.pdf")
    assert tool(client, auth, "protect", document_id=d, password="123").status_code == 422      # too short
    locked = tool(client, auth, "protect", document_id=d, password="open sesame")
    assert locked.status_code == 201, locked.text
    locked = locked.json()
    assert locked["processing_status"] == "encrypted" and locked["original_filename"] == "plan (protected).pdf"
    assert pdf(client, auth, locked["id"], "open sesame").pages[0].extract_text().strip() == "Secret"
    assert tool(client, auth, "merge", document_ids=[d, locked["id"]]).json()["error"]["code"] == "document_encrypted"
    wrong = tool(client, auth, "unlock", document_id=locked["id"], password="guess")
    assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "wrong_password"
    opened = tool(client, auth, "unlock", document_id=locked["id"], password="open sesame")
    assert opened.status_code == 201, opened.text
    assert opened.json()["processing_status"] == "ready" and texts(client, auth, opened.json()["id"]) == ["Secret"]
    assert tool(client, auth, "unlock", document_id=d, password="x").status_code == 422          # not protected
    # A file that opens freely but restricts editing is not unlocked with an empty or user password.
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf(["Owner only"]))))
    w.encrypt(user_password="", owner_password="the-owner", algorithm="AES-256")
    buf = io.BytesIO(); w.write(buf)
    restricted = upload(client, auth, buf.getvalue()).json()["id"]
    assert tool(client, auth, "unlock", document_id=restricted, password="anything").status_code == 403
    assert tool(client, auth, "unlock", document_id=restricted, password="the-owner").status_code == 201


def test_compress_keeps_content_and_only_saves_smaller_files(client, auth):
    small = make(client, auth, ["Tiny"])
    r = tool(client, auth, "compress", document_id=small, level="lossless")
    assert r.status_code == 200 and r.json()["document"] is None                                 # nothing to gain
    photo = io.BytesIO()
    Image.effect_noise((1600, 1600), 80).convert("RGB").save(photo, "PNG")
    big = client.post("/api/tools/images-to-pdf", headers=auth, data={"page_size": "fit", "name": "Photo"},
                      files=[("files", ("p.png", photo.getvalue(), "image/png"))])
    assert big.status_code == 201, big.text
    r = tool(client, auth, "compress", document_id=big.json()["id"], level="strong")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["document"] and out["after"] < out["before"] * 0.8
    assert len(pdf(client, auth, out["document"]["id"]).pages[0].images) == 1


def test_images_to_pdf_and_back_to_images_and_text(client, auth):
    def png(color, size):
        buf = io.BytesIO(); Image.new("RGB", size, color).save(buf, "PNG"); return buf.getvalue()

    r = client.post("/api/tools/images-to-pdf", headers=auth, data={"page_size": "a4", "name": "Scans"},
                    files=[("files", ("a.png", png("red", (400, 200)), "image/png")), ("files", ("b.png", png("blue", (200, 400)), "image/png"))])
    assert r.status_code == 201, r.text
    doc = r.json()
    pages = pdf(client, auth, doc["id"]).pages
    assert doc["page_count"] == 2 and round(float(pages[0].mediabox.width)) == 842 and round(float(pages[1].mediabox.width)) == 595
    bad = client.post("/api/tools/images-to-pdf", headers=auth, files=[("files", ("x.png", b"not an image", "image/png"))])
    assert bad.status_code == 422

    z = tool(client, auth, "to-images", document_id=doc["id"], dpi=72, format="png")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(z.content))
    assert archive.namelist() == ["page-001.png", "page-002.png"]
    first = Image.open(io.BytesIO(archive.read("page-001.png")))
    assert first.width == 842 and abs(first.height - 595) <= 1 and first.convert("RGB").getpixel((421, 297)) == (255, 0, 0)

    t = tool(client, auth, "to-text", document_id=make(client, auth, ["Hello", "World"]))
    assert t.status_code == 200 and t.text == "--- Page 1 ---\nHello\n\n--- Page 2 ---\nWorld\n\n"


def test_stamp_watermark_crop_and_metadata_add_versions(client, auth):
    d = make(client, auth, ["One", "Two", "Three"])
    r = tool(client, auth, "stamp", document_id=d, text="Page {n} of {total}", position="bottom-right", pages="2-3", start=1)
    assert r.status_code == 200 and r.json()["current_version"] == 2, r.text
    assert texts(client, auth, d)[0] == "One"
    assert "Page 1 of 2" in texts(client, auth, d)[1] and "Page 2 of 2" in texts(client, auth, d)[2]
    assert tool(client, auth, "stamp", document_id=d, pages="2-9").status_code == 422
    assert tool(client, auth, "stamp", document_id=d, pages="two").status_code == 422
    assert tool(client, auth, "watermark", document_id=d, text="DRAFT", angle=45, opacity=0.2).status_code == 200
    assert all("DRAFT" in t for t in texts(client, auth, d))

    r = tool(client, auth, "crop", document_id=d, left=10, top=20, right=30, bottom=40, pages="1")
    assert r.status_code == 200, r.text
    first, second = pdf(client, auth, d).pages[:2]
    assert [round(float(v)) for v in first.cropbox] == [10, 40, 582, 772] and [round(float(v)) for v in second.cropbox] == [0, 0, 612, 792]
    assert tool(client, auth, "crop", document_id=d).status_code == 422
    assert tool(client, auth, "crop", document_id=d, left=600).status_code == 422

    assert tool(client, auth, "metadata", document_id=d, title="Quarterly plan", author="Asha").status_code == 200
    meta = pdf(client, auth, d).metadata
    assert meta.title == "Quarterly plan" and meta.author == "Asha"
    assert client.get(f"/api/documents/{d}", headers=auth).json()["current_version"] == 5
    assert client.post(f"/api/editor/{d}/undo", headers=auth).status_code == 200
    assert pdf(client, auth, d).metadata.title != "Quarterly plan"


def test_crop_follows_rotation(client, auth):
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf(["R"], size=(600, 800)))))
    w.pages[0].rotate(90)
    buf = io.BytesIO(); w.write(buf)
    d = upload(client, auth, buf.getvalue()).json()["id"]
    assert tool(client, auth, "crop", document_id=d, left=10, top=20, right=30, bottom=40).status_code == 200
    # shown rotated 90 degrees clockwise: displayed left is the bottom of the unrotated page, displayed top is its left
    assert [round(float(v)) for v in pdf(client, auth, d).pages[0].cropbox] == [20, 10, 560, 770]


def test_form_fields_can_be_listed_and_filled(client, auth):
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 750, "Form")
    c.acroForm.textfield(name="full_name", x=72, y=700, width=200, height=20, value="")
    c.acroForm.checkbox(name="agree", x=72, y=660, size=14, checked=False)
    c.acroForm.choice(name="plan", x=72, y=620, width=120, height=20, options=["Basic", "Pro"], value="Basic")
    c.save()
    d = upload(client, auth, buf.getvalue()).json()["id"]
    fields = {f["name"]: f for f in client.get(f"/api/tools/form/{d}", headers=auth).json()["fields"]}
    assert fields["full_name"]["type"] == "text" and fields["agree"]["type"] == "checkbox" and fields["agree"]["value"] is False
    assert fields["plan"]["type"] == "choice" and fields["plan"]["options"] == ["Basic", "Pro"]
    r = tool(client, auth, "form", document_id=d, values={"full_name": "Asha Rao", "agree": True, "plan": "Pro", "ghost": "x"})
    assert r.status_code == 200 and r.json()["current_version"] == 2, r.text
    filled = {f["name"]: f["value"] for f in client.get(f"/api/tools/form/{d}", headers=auth).json()["fields"]}
    assert filled == {"full_name": "Asha Rao", "agree": True, "plan": "Pro"}
    assert tool(client, auth, "form", document_id=d, values={"ghost": "x"}).status_code == 422
    plain = make(client, auth, ["No form"])
    assert client.get(f"/api/tools/form/{plain}", headers=auth).json()["fields"] == []


def test_link_items_become_link_annotations(client, auth):
    d = make(client, auth, ["Links"])
    bad = client.post("/api/edit/apply", headers=auth, json={"document_id": d, "items": [
        {"type": "link", "page": 1, "x": 10, "y": 10, "w": 50, "h": 20, "url": "javascript:alert(1)"}]})
    assert bad.status_code == 422
    r = client.post("/api/edit/apply", headers=auth, json={"document_id": d, "items": [
        {"type": "link", "page": 1, "x": 72, "y": 80, "w": 100, "h": 30, "url": "https://example.com/page"}]})
    assert r.status_code == 200, r.text
    annots = [a.get_object() for a in pdf(client, auth, d).pages[0]["/Annots"]]
    assert len(annots) == 1 and annots[0]["/Subtype"] == "/Link" and annots[0]["/A"]["/URI"] == "https://example.com/page"
    assert [round(float(v)) for v in annots[0]["/Rect"]] == [72, 682, 172, 712]


def test_bates_nup_resize_mix_and_bookmark_split(client, auth):
    d = make(client, auth, ["One", "Two", "Three"], "case.pdf")
    assert tool(client, auth, "bates", document_id=d, prefix="ABC-", start=7, digits=5).status_code == 200
    assert ["ABC-00007" in t and "ABC-00008" not in t for t in texts(client, auth, d)][0]
    assert "ABC-00009" in texts(client, auth, d)[2]

    two = tool(client, auth, "nup", document_id=d, per_sheet=2)
    assert two.status_code == 201, two.text
    sheets = pdf(client, auth, two.json()["id"]).pages
    assert len(sheets) == 2 and round(float(sheets[0].mediabox.width)) == 842
    assert "One" in sheets[0].extract_text() and "Two" in sheets[0].extract_text() and "Three" in sheets[1].extract_text()
    assert tool(client, auth, "nup", document_id=d, per_sheet=4).json()["page_count"] == 1
    assert tool(client, auth, "nup", document_id=d, per_sheet=3).status_code == 422

    assert tool(client, auth, "resize", document_id=d, size="a5").status_code == 200
    page = pdf(client, auth, d).pages[0]
    assert [round(float(page.mediabox.width)), round(float(page.mediabox.height))] == [420, 595] and "One" in page.extract_text()

    other = make(client, auth, ["X1", "X2"])
    mixed = tool(client, auth, "mix", document_ids=[other, d], name="Zip")
    assert mixed.status_code == 201, mixed.text
    assert [t.split()[0] if i % 2 == 0 or i == 4 else t for i, t in enumerate(texts(client, auth, mixed.json()["id"]))][0] == "X1"
    assert len(pdf(client, auth, mixed.json()["id"]).pages) == 5

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf(["Intro", "Ch1 a", "Ch1 b", "Ch2"]))))
    w.add_outline_item("Chapter 1", 1)
    w.add_outline_item("Chapter 2", 3)
    buf = io.BytesIO(); w.write(buf)
    book = upload(client, auth, buf.getvalue(), "book.pdf").json()["id"]
    parts = tool(client, auth, "split", document_id=book, mode="bookmarks")
    assert parts.status_code == 201, parts.text
    assert [texts(client, auth, p["id"]) for p in parts.json()["documents"]] == [["Intro"], ["Ch1 a", "Ch1 b"], ["Ch2"]]
    assert tool(client, auth, "split", document_id=other, mode="bookmarks").status_code == 422


def test_grayscale_flatten_annotations_images_and_repair(client, auth):
    photo = io.BytesIO()
    Image.new("RGB", (300, 200), (220, 30, 30)).save(photo, "PNG")
    doc = client.post("/api/tools/images-to-pdf", headers=auth, data={"page_size": "fit", "name": "Red"},
                      files=[("files", ("p.png", photo.getvalue(), "image/png"))]).json()["id"]
    z = tool(client, auth, "extract-images", document_id=doc)
    assert z.status_code == 200, z.text
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert len(names) == 1 and names[0].startswith("page-001-image-001.")
    assert tool(client, auth, "extract-images", document_id=make(client, auth, ["No pictures"])).status_code == 422

    grey = tool(client, auth, "grayscale", document_id=doc, dpi=100)
    assert grey.status_code == 201, grey.text
    picture = pdf(client, auth, grey.json()["id"]).pages[0].images[0].image.convert("RGB")
    r, g, b = picture.getpixel((picture.width // 2, picture.height // 2))
    assert abs(r - g) < 6 and abs(g - b) < 6 and r < 200          # the red page is now a mid grey

    d = make(client, auth, ["Annotated"])
    client.post("/api/edit/apply", headers=auth, json={"document_id": d, "items": [
        {"type": "link", "page": 1, "x": 72, "y": 80, "w": 100, "h": 30, "url": "https://example.com"},
        {"type": "field", "page": 1, "x": 72, "y": 200, "w": 200, "h": 22, "field": "text", "name": "City"},
        {"type": "field", "page": 1, "x": 72, "y": 240, "w": 16, "h": 16, "field": "checkbox", "name": "Agree"},
        {"type": "field", "page": 1, "x": 72, "y": 280, "w": 200, "h": 22, "field": "text", "name": "City"}]})
    fields = {f["name"]: f["type"] for f in client.get(f"/api/tools/form/{d}", headers=auth).json()["fields"]}
    assert fields == {"City": "text", "Agree": "checkbox", "City 2": "text"}          # duplicate names are made unique
    assert tool(client, auth, "form", document_id=d, values={"City": "Jaipur", "Agree": True}).status_code == 200
    filled = {f["name"]: f["value"] for f in client.get(f"/api/tools/form/{d}", headers=auth).json()["fields"]}
    assert filled["City"] == "Jaipur" and filled["Agree"] is True
    bad = client.post("/api/edit/apply", headers=auth, json={"document_id": d, "items": [
        {"type": "field", "page": 1, "x": 1, "y": 1, "w": 50, "h": 20, "name": "a.b"}]})
    assert bad.status_code == 422

    assert tool(client, auth, "remove-annotations", document_id=d, what="links").status_code == 200
    kinds = [a.get_object()["/Subtype"] for a in pdf(client, auth, d).pages[0]["/Annots"]]
    assert kinds == ["/Widget", "/Widget", "/Widget"]
    assert tool(client, auth, "remove-annotations", document_id=d, what="comments").status_code == 422   # none left of that kind
    assert tool(client, auth, "flatten", document_id=d).status_code == 200
    flat = pdf(client, auth, d)
    assert not flat.get_fields() and "Jaipur" in flat.pages[0].extract_text() and "Annotated" in flat.pages[0].extract_text()

    # repair: a file whose cross-reference table is cut off is rejected by upload but rebuilt by the tool
    good = build_pdf(["Still here"])
    broken = good[:good.rindex(b"xref")]
    assert upload(client, auth, broken, "hurt.pdf").status_code in (201, 422)
    fixed = client.post("/api/tools/repair", headers=auth, files={"file": ("hurt.pdf", broken, "application/pdf")})
    assert fixed.status_code == 201, fixed.text
    assert fixed.json()["original_filename"] == "hurt (repaired).pdf" and texts(client, auth, fixed.json()["id"]) == ["Still here"]
    junk = client.post("/api/tools/repair", headers=auth, files={"file": ("x.pdf", b"%PDF-1.4 nothing useful", "application/pdf")})
    assert junk.status_code == 422
