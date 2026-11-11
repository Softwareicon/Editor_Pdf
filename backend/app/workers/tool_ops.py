"""Stand-alone worker for the PDF tools (runs sandboxed, see services/sandbox.py).

Usage: python tool_ops.py <operation>      (request JSON on stdin, one JSON object on stdout)
The request names its input and output files; the worker never chooses a path itself.
"""
import base64
import io
import json
import sys
import zipfile

MAX_IMAGE_PIXELS = 40_000_000


def _reader(path, password=None):
    from pypdf import PdfReader

    reader = PdfReader(path, strict=False)
    if reader.is_encrypted:
        raise _Refused("encrypted")
    return reader


class _Refused(Exception):
    def __init__(self, error, detail=""):
        super().__init__(error)
        self.error, self.detail = error, detail


def _save(writer, out):
    with open(out, "wb") as f:
        writer.write(f)


def merge(req):
    from pypdf import PdfWriter

    writer = PdfWriter()
    total = 0
    for path in req["inputs"]:
        reader = _reader(path)
        total += len(reader.pages)
        if total > req["max_pages"]:
            raise _Refused("bad_request", "the merged document would have too many pages")
        writer.append(reader, import_outline=True)
    _save(writer, req["out"])
    return {"page_count": total}


def split(req):
    from pypdf import PdfWriter

    reader = _reader(req["inputs"][0])
    count = len(reader.pages)
    for group, out in zip(req["groups"], req["outs"]):
        if not group or any(not (isinstance(i, int) and 0 <= i < count) for i in group):
            raise _Refused("bad_request", "page out of range")
        writer = PdfWriter()
        writer.append(reader, pages=group, import_outline=False)
        if reader.metadata:
            writer.add_metadata({k: v for k, v in reader.metadata.items() if isinstance(v, str)})
        _save(writer, out)
    return {"parts": len(req["groups"])}


def compress(req):
    from pypdf import PdfWriter

    quality = req.get("image_quality")          # None keeps images untouched (lossless)
    max_side = req.get("max_image_side")
    writer = PdfWriter(clone_from=_reader(req["inputs"][0]))
    images = 0
    for page in writer.pages:
        if quality:
            for img in page.images:
                try:
                    pil = img.image
                    if pil is None or pil.width * pil.height > MAX_IMAGE_PIXELS or pil.mode in ("1", "P", "RGBA", "LA"):
                        continue                 # masks, palettes and transparency are left alone
                    if max_side and max(pil.size) > max_side:
                        pil.thumbnail((max_side, max_side))
                    img.replace(pil, quality=quality)
                    images += 1
                except Exception:               # an image pypdf cannot re-encode stays as it is
                    continue
        page.compress_content_streams()
    writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
    _save(writer, req["out"])
    return {"images": images, "page_count": len(writer.pages)}


def protect(req):
    from pypdf import PdfWriter

    writer = PdfWriter(clone_from=_reader(req["inputs"][0]))
    writer.encrypt(user_password=req["password"], owner_password=req["password"], algorithm="AES-256")
    _save(writer, req["out"])
    return {}


def unlock(req):
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(req["inputs"][0], strict=False)
    if not reader.is_encrypted:
        raise _Refused("bad_request", "this document is not password protected")
    opens_without_password = bool(reader.decrypt(""))
    result = int(reader.decrypt(req["password"]))
    # A file that opens freely but restricts editing is unlocked only with its owner password (result 2).
    if result == 0 or (opens_without_password and result != 2):
        raise _Refused("wrong_password")
    writer = PdfWriter(clone_from=reader)
    _save(writer, req["out"])
    return {"page_count": len(writer.pages)}


def crop(req):
    from pypdf import PdfWriter
    from pypdf.generic import RectangleObject

    writer = PdfWriter(clone_from=_reader(req["inputs"][0]))
    left, top, right, bottom = (float(req[k]) for k in ("left", "top", "right", "bottom"))
    for n in req["pages"]:
        page = writer.pages[n - 1]
        box = page.cropbox
        x0, y0, x1, y1 = float(box.left), float(box.bottom), float(box.right), float(box.top)
        rotation = int(page.rotation or 0) % 360   # margins are given for the page as it is displayed
        if rotation == 90:
            new = (x0 + top, y0 + left, x1 - bottom, y1 - right)
        elif rotation == 180:
            new = (x0 + right, y0 + top, x1 - left, y1 - bottom)
        elif rotation == 270:
            new = (x0 + bottom, y0 + right, x1 - top, y1 - left)
        else:
            new = (x0 + left, y0 + bottom, x1 - right, y1 - top)
        if new[2] - new[0] < 36 or new[3] - new[1] < 36:
            raise _Refused("bad_request", f"the margins leave almost nothing of page {n}")
        page.cropbox = RectangleObject(new)
    _save(writer, req["out"])
    return {"page_count": len(writer.pages)}


def metadata(req):
    from pypdf import PdfWriter

    writer = PdfWriter(clone_from=_reader(req["inputs"][0]))
    writer.add_metadata({f"/{key.capitalize()}": str(value) for key, value in req["values"].items()})
    _save(writer, req["out"])
    return {"page_count": len(writer.pages)}


def images_to_pdf(req):
    from PIL import Image, ImageOps
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    c = canvas.Canvas(req["out"], pageCompression=1)
    for data in req["images"]:
        raw = base64.b64decode(data)
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.format not in ("PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF"):
                raise _Refused("bad_request", "unsupported image type")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
            img.load()
        if req.get("page_size") == "a4":
            pw, ph = (841.89, 595.28) if img.width > img.height else (595.28, 841.89)
            scale = min((pw - 72) / img.width, (ph - 72) / img.height)
        else:
            scale = min(0.75, 14400 / img.width, 14400 / img.height)   # 96 dpi image pixels -> points
            pw, ph = img.width * scale, img.height * scale
        w, h = img.width * scale, img.height * scale
        c.setPageSize((pw, ph))
        c.drawImage(ImageReader(img), (pw - w) / 2, (ph - h) / 2, w, h, mask="auto")
        c.showPage()
    c.save()
    return {"page_count": len(req["images"])}


def to_images(req):
    import pypdfium2 as pdfium

    _reader(req["inputs"][0])                    # refuses encrypted files the same way as every other tool
    pdf = pdfium.PdfDocument(req["inputs"][0])
    fmt = "PNG" if req.get("format") == "png" else "JPEG"
    count = len(pdf)
    if count > req["max_pages"]:
        raise _Refused("bad_request", f"at most {req['max_pages']} pages can be converted at once")
    with zipfile.ZipFile(req["out"], "w", zipfile.ZIP_STORED) as z:
        for i in range(count):
            page = pdf[i]
            w, h = page.get_size()
            scale = min(req["dpi"] / 72, (25_000_000 / max(w * h, 1)) ** 0.5)
            image = page.render(scale=scale).to_pil()
            if fmt == "JPEG":
                image = image.convert("RGB")
            buf = io.BytesIO()
            image.save(buf, fmt, quality=88)
            z.writestr(f"page-{i + 1:03d}.{'png' if fmt == 'PNG' else 'jpg'}", buf.getvalue())
            page.close()
    return {"page_count": count}


def to_text(req):
    reader = _reader(req["inputs"][0])
    with open(req["out"], "w", encoding="utf-8") as f:
        for i, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            f.write(f"--- Page {i} ---\n{text.strip()}\n\n")
    return {"page_count": len(reader.pages)}


def _field_info(name, field):
    kind = field.get("/FT")
    flags = int(field.get("/Ff", 0) or 0)
    value = field.get("/V")
    value = "" if value is None else str(value)
    if kind == "/Tx":
        return {"name": name, "type": "multiline" if flags & 4096 else "text", "value": value}
    if kind == "/Ch":
        options = [str(o[1] if isinstance(o, (list, tuple)) and len(o) > 1 else o) for o in (field.get("/Opt") or [])]
        return {"name": name, "type": "choice", "value": value, "options": options[:500]}
    if kind == "/Btn" and not flags & 65536:      # push buttons carry no value
        states = [str(s) for s in (field.get("/_States_") or []) if str(s) != "/Off"]
        if flags & 32768:
            return {"name": name, "type": "radio", "value": value.lstrip("/"), "options": [s.lstrip("/") for s in states]}
        return {"name": name, "type": "checkbox", "value": value not in ("", "/Off"), "on": (states or ["/Yes"])[0]}
    return None


def form_fields(req):
    reader = _reader(req["inputs"][0])
    fields = []
    for name, field in (reader.get_fields() or {}).items():
        info = _field_info(str(name), field)
        if info and "/Kids" in field and field.get("/FT") is None:
            continue
        if info:
            fields.append(info)
        if len(fields) >= 1000:
            break
    return {"fields": fields}


def form_fill(req):
    from pypdf import PdfWriter
    from pypdf.generic import NameObject

    reader = _reader(req["inputs"][0])
    known = {str(n): _field_info(str(n), f) for n, f in (reader.get_fields() or {}).items()}
    values = {}
    for name, value in req["values"].items():
        info = known.get(name)
        if not info:
            continue
        if info["type"] == "checkbox":
            values[name] = NameObject(info["on"] if value else "/Off")
        elif info["type"] == "radio":
            values[name] = NameObject("/" + str(value)) if value else NameObject("/Off")
        else:
            values[name] = str(value)
    if not values:
        raise _Refused("bad_request", "none of the given fields exist in this form")
    writer = PdfWriter(clone_from=reader)
    writer.set_need_appearances_writer(True)
    for page in writer.pages:
        if page.get("/Annots"):
            writer.update_page_form_field_values(page, values, auto_regenerate=False)
    _save(writer, req["out"])
    return {"page_count": len(writer.pages), "filled": len(values)}


OPERATIONS = {f.__name__: f for f in (merge, split, compress, protect, unlock, crop, metadata, images_to_pdf,
                                      to_images, to_text, form_fields, form_fill)}


def run(operation: str, req: dict) -> dict:
    from pypdf.errors import PyPdfError

    try:
        return {"ok": True, **OPERATIONS[operation](req)}
    except _Refused as exc:
        return {"ok": False, "error": exc.error, "detail": exc.detail}
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError, OSError,
            AssertionError, SyntaxError, zipfile.BadZipFile) as exc:
        return {"ok": False, "error": "failed", "detail": f"{type(exc).__name__}: {exc}"[:200]}
    except MemoryError:
        return {"ok": False, "error": "failed", "detail": "memory limit exceeded"}


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1], json.loads(sys.stdin.read()))))
