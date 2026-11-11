"""Builds small, valid PDFs from scratch (no third-party code or content, so freely distributable)."""
from __future__ import annotations

import io


def build_pdf(pages: list[str], size: tuple[int, int] = (612, 792), title: str = "PDF Master Pro sample") -> bytes:
    """Return a PDF with one line of Helvetica text per page."""
    objects: list[bytes] = []

    def add(body: str | bytes) -> int:
        objects.append(body if isinstance(body, bytes) else body.encode("latin-1"))
        return len(objects)

    def esc(text: str) -> str:
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    catalog = add("")  # placeholders, filled in below
    pages_obj = add("")
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    kids = []
    for text in pages:
        stream = f"BT /F1 24 Tf 72 {size[1] - 100} Td ({esc(text)}) Tj ET".encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 {size[0]} {size[1]}] "
                        f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"))
    info = add(f"<< /Title ({esc(title)}) /Producer (PDF Master Pro test factory) >>")
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    objects[pages_obj - 1] = (f"<< /Type /Pages /Count {len(kids)} /Kids "
                              f"[{' '.join(f'{k} 0 R' for k in kids)}] >>").encode()

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objects) + 1, catalog, info, xref))
    return out.getvalue()


def build_encrypted_pdf(user_password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(build_pdf(["Secret page"]))))
    writer.encrypt(user_password=user_password, owner_password="owner-" + user_password, algorithm="AES-256")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


if __name__ == "__main__":  # python tests/pdf_factory.py -> regenerates samples/
    from pathlib import Path

    samples = Path(__file__).resolve().parents[1] / "samples"
    samples.mkdir(exist_ok=True)
    (samples / "sample-3-pages.pdf").write_bytes(
        build_pdf(["Page one: the quick brown fox", "Page two: jumps over", "Page three: the lazy dog"]))
    (samples / "sample-landscape.pdf").write_bytes(build_pdf(["Landscape page"], size=(792, 612)))
    (samples / "sample-encrypted-password-is-test1234.pdf").write_bytes(build_encrypted_pdf("test1234"))
    print("Wrote samples to", samples)
