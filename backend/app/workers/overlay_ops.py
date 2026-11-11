"""Stand-alone overlay worker (runs sandboxed, see services/sandbox.py).

Usage: python overlay_ops.py <input.pdf> <output.pdf>     (edit JSON on stdin: {"items": [...]})

Draws text, shapes, lines, freehand ink and images on top of existing pages. The original page
content is left untouched underneath (a "whiteout" is an opaque rectangle; it hides, it does not
remove). Item coordinates are in points in the displayed page: origin top-left, y downwards.
"""
import base64
import io
import json
import math
import sys

FONTS = {
    "helvetica": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"),
    "times": ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"),
    "courier": ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique"),
}
LINE_HEIGHT = 1.2   # must match the editor (frontend/assets/js/edit-tools.js)
ASCENT = 0.9
MAX_IMAGE_PIXELS = 40_000_000


def _rgb(value: str):
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def _image(data: str):
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    raw = base64.b64decode(data, validate=True)
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with Image.open(io.BytesIO(raw)) as probe:
        if probe.format not in ("PNG", "JPEG"):
            raise ValueError("unsupported image type")
        if probe.width * probe.height > MAX_IMAGE_PIXELS:
            raise ValueError("image too large")
        probe.verify()
    with Image.open(io.BytesIO(raw)) as img:
        if img.format == "JPEG" and img.mode in ("RGB", "L"):
            return ImageReader(io.BytesIO(raw))  # embedded as-is, no recompression
        img = img.convert("RGBA")
        img.load()
    return ImageReader(img)


def _draw(c, item: dict, height: float) -> None:
    kind = item["type"]
    c.saveState()
    if kind == "text":
        bold, italic = bool(item.get("bold")), bool(item.get("italic"))
        font = FONTS[item.get("font", "helvetica")][(1 if bold else 0) + (2 if italic else 0)]
        size = float(item["size"])
        c.setFont(font, size)
        c.setFillColorRGB(*_rgb(item["color"]))
        for i, line in enumerate(item["text"].split("\n")):
            if line:
                c.drawString(item["x"], height - (item["y"] + ASCENT * size + i * LINE_HEIGHT * size), line)
    elif kind in ("rect", "ellipse"):
        fill, stroke = item.get("fill"), item.get("stroke")
        alpha = float(item.get("opacity", 1))
        if alpha < 1:
            c.setFillAlpha(alpha)
            c.setStrokeAlpha(alpha)
        if fill:
            c.setFillColorRGB(*_rgb(fill))
        if stroke:
            c.setStrokeColorRGB(*_rgb(stroke))
            c.setLineWidth(float(item.get("stroke_width", 1.5)))
        x, y, w, h = item["x"], height - item["y"] - item["h"], item["w"], item["h"]
        if kind == "rect":
            c.rect(x, y, w, h, stroke=1 if stroke else 0, fill=1 if fill else 0)
        else:
            c.ellipse(x, y, x + w, y + h, stroke=1 if stroke else 0, fill=1 if fill else 0)
    elif kind == "line":
        x1, y1, x2, y2 = item["x1"], height - item["y1"], item["x2"], height - item["y2"]
        width = float(item.get("width", 1.5))
        c.setStrokeColorRGB(*_rgb(item["color"]))
        c.setFillColorRGB(*_rgb(item["color"]))
        c.setLineWidth(width)
        c.setLineCap(1)
        length = math.hypot(x2 - x1, y2 - y1)
        if item.get("arrow") and length > 0:
            head = min(max(4 * width, 8), length)
            ux, uy = (x2 - x1) / length, (y2 - y1) / length
            bx, by = x2 - ux * head, y2 - uy * head
            c.line(x1, y1, bx + ux * 0.5, by + uy * 0.5)
            p = c.beginPath()
            p.moveTo(x2, y2)
            p.lineTo(bx - uy * head * 0.45, by + ux * head * 0.45)
            p.lineTo(bx + uy * head * 0.45, by - ux * head * 0.45)
            p.close()
            c.drawPath(p, stroke=0, fill=1)
        else:
            c.line(x1, y1, x2, y2)
    elif kind == "ink":
        c.setStrokeColorRGB(*_rgb(item["color"]))
        c.setLineWidth(float(item.get("width", 1.5)))
        c.setLineCap(1)
        c.setLineJoin(1)
        p = c.beginPath()
        (x0, y0), rest = item["points"][0], item["points"][1:]
        p.moveTo(x0, height - y0)
        for x, y in rest:
            p.lineTo(x, height - y)
        c.drawPath(p, stroke=1, fill=0)
    elif kind == "image":
        c.drawImage(_image(item["data"]), item["x"], height - item["y"] - item["h"], item["w"], item["h"], mask="auto")
    else:
        raise ValueError(f"unknown item type {kind!r}")
    c.restoreState()


def _placement(page):
    """Displayed (width, height) of a page and the matrix mapping overlay space onto it."""
    box = page.cropbox
    x0, y0, x1, y1 = float(box.left), float(box.bottom), float(box.right), float(box.top)
    w, h = x1 - x0, y1 - y0
    rotation = int(page.rotation or 0) % 360
    if rotation == 90:
        return h, w, (0, 1, -1, 0, x1, y0)
    if rotation == 180:
        return w, h, (-1, 0, 0, -1, x1, y1)
    if rotation == 270:
        return h, w, (0, -1, 1, 0, x0, y1)
    return w, h, (1, 0, 0, 1, x0, y0)


def apply(src_path: str, out_path: str, spec: dict) -> dict:
    from pypdf import PdfReader, PdfWriter, Transformation
    from pypdf.errors import PyPdfError

    try:
        from reportlab.pdfgen import canvas

        reader = PdfReader(src_path, strict=False)
        if reader.is_encrypted:
            return {"ok": False, "error": "encrypted"}
        count = len(reader.pages)
        by_page: dict[int, list[dict]] = {}
        for item in spec["items"]:
            n = item["page"]
            if not (isinstance(n, int) and 1 <= n <= count):
                return {"ok": False, "error": "bad_request", "detail": f"the document has only {count} pages"}
            by_page.setdefault(n, []).append(item)
        if not by_page:
            return {"ok": False, "error": "bad_request", "detail": "nothing to apply"}

        writer = PdfWriter(clone_from=reader)
        order = sorted(by_page)
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pageCompression=1)
        placements = {}
        for n in order:
            width, height, ctm = _placement(writer.pages[n - 1])
            placements[n] = ctm
            c.setPageSize((width, height))
            for item in by_page[n]:
                try:
                    _draw(c, item, height)
                except (ValueError, KeyError, TypeError, OSError, SyntaxError) as exc:
                    detail = "an image could not be read (use PNG or JPEG)" if item.get("type") == "image" else "invalid item"
                    return {"ok": False, "error": "bad_request", "detail": f"{detail}: {type(exc).__name__}"[:200]}
            c.showPage()
        c.save()
        overlay = PdfReader(io.BytesIO(buf.getvalue()))
        for i, n in enumerate(order):
            writer.pages[n - 1].merge_transformed_page(overlay.pages[i], Transformation(placements[n]), over=True)
        with open(out_path, "wb") as f:
            writer.write(f)
        return {"ok": True, "page_count": count, "items": len(spec["items"])}
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, IndexError,
            RecursionError, OSError, AssertionError) as exc:
        return {"ok": False, "error": "failed", "detail": f"{type(exc).__name__}: {exc}"[:200]}
    except MemoryError:
        return {"ok": False, "error": "failed", "detail": "memory limit exceeded"}


if __name__ == "__main__":
    print(json.dumps(apply(sys.argv[1], sys.argv[2], json.loads(sys.stdin.read()))))
