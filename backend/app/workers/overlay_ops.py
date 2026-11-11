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
        if float(item.get("opacity", 1)) < 1:
            c.setFillAlpha(float(item["opacity"]))
        # (x, y) is the top of the first line; x is its left edge, centre or right edge depending on the alignment.
        c.translate(item["x"], height - (item["y"] + ASCENT * size))
        if item.get("rotate"):
            c.rotate(float(item["rotate"]))
        draw = {"center": c.drawCentredString, "right": c.drawRightString}.get(item.get("align"), c.drawString)
        for i, line in enumerate(item["text"].split("\n")):
            if line:
                draw(0, -i * LINE_HEIGHT * size, line)
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
    elif kind in ("link", "field"):
        pass  # added as annotations after the overlay is merged
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


def _add_fields(writer, specs, taken) -> None:
    """Create AcroForm text fields and check boxes. specs: [(page_index, (x0, y0, x1, y1), item)]."""
    from pypdf.generic import (ArrayObject, BooleanObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject,
                               NumberObject, TextStringObject)

    def name(value):
        return NameObject(value)

    def font(base):
        return writer._add_object(DictionaryObject({name("/Type"): name("/Font"), name("/Subtype"): name("/Type1"),
                                                    name("/BaseFont"): name(base)}))

    root = writer._root_object
    if "/AcroForm" not in root:
        root[name("/AcroForm")] = writer._add_object(DictionaryObject())
    form = root["/AcroForm"].get_object()
    if "/Fields" not in form:
        form[name("/Fields")] = ArrayObject()
    if "/DR" not in form:
        form[name("/DR")] = DictionaryObject()
    resources = form["/DR"].get_object()
    if "/Font" not in resources:
        resources[name("/Font")] = DictionaryObject()
    fonts = resources["/Font"].get_object()
    if "/Helv" not in fonts:
        fonts[name("/Helv")] = font("/Helvetica")
    if "/ZaDb" not in fonts:
        fonts[name("/ZaDb")] = font("/ZapfDingbats")
    if "/DA" not in form:
        form[name("/DA")] = TextStringObject("/Helv 0 Tf 0 g")
    form[name("/NeedAppearances")] = BooleanObject(True)

    for page_index, rect, item in specs:
        label, n = item["name"], 2
        while label in taken:                       # field names must be unique in a document
            label, n = f"{item['name']} {n}", n + 1
        taken.add(label)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        widget = DictionaryObject({
            name("/Type"): name("/Annot"), name("/Subtype"): name("/Widget"), name("/T"): TextStringObject(label),
            name("/Rect"): ArrayObject(FloatObject(round(v, 2)) for v in rect), name("/F"): NumberObject(4),
            name("/MK"): DictionaryObject({name("/BC"): ArrayObject(FloatObject(v) for v in (0.45, 0.45, 0.5))}),
            name("/BS"): DictionaryObject({name("/W"): NumberObject(1), name("/S"): name("/S")}),
        })
        if item["field"] == "checkbox":
            def appearance(content: str):
                stream = DecodedStreamObject()
                stream.set_data(content.encode("latin-1"))
                stream.update({name("/Type"): name("/XObject"), name("/Subtype"): name("/Form"),
                               name("/BBox"): ArrayObject(FloatObject(v) for v in (0, 0, round(w, 2), round(h, 2))),
                               name("/Resources"): DictionaryObject({name("/Font"): DictionaryObject({name("/ZaDb"): fonts["/ZaDb"]})})})
                return writer._add_object(stream)

            size = min(w, h) * 0.8
            tick = f"q BT /ZaDb {size:.2f} Tf 0 g {(w - size * 0.76) / 2:.2f} {(h - size * 0.7) / 2:.2f} Td (4) Tj ET Q"
            widget.update({name("/FT"): name("/Btn"), name("/V"): name("/Off"), name("/AS"): name("/Off"),
                           name("/DA"): TextStringObject("/ZaDb 0 Tf 0 g"),
                           name("/AP"): DictionaryObject({name("/N"): DictionaryObject({name("/Yes"): appearance(tick),
                                                                                         name("/Off"): appearance("")})})})
            widget["/MK"][name("/CA")] = TextStringObject("4")
        else:
            flags = 4096 if item["field"] == "multiline" else 0
            widget.update({name("/FT"): name("/Tx"), name("/V"): TextStringObject(""), name("/Ff"): NumberObject(flags),
                           name("/DA"): TextStringObject(f"/Helv {0 if flags else min(12, max(6, round(h * 0.6)))} Tf 0 g")})
        added = writer.add_annotation(page_number=page_index, annotation=widget)
        form["/Fields"].append(added.indirect_reference)


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
        placements, heights = {}, {}
        for n in order:
            width, height, ctm = _placement(writer.pages[n - 1])
            placements[n], heights[n] = ctm, height
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
        from pypdf.annotations import Link

        new_fields = []
        for i, n in enumerate(order):
            if any(item["type"] not in ("link", "field") for item in by_page[n]):
                writer.pages[n - 1].merge_transformed_page(overlay.pages[i], Transformation(placements[n]), over=True)
            a, b, cc, d, e, f = placements[n]
            height = heights[n]
            for item in by_page[n]:
                if item["type"] not in ("link", "field"):
                    continue
                corners = [(item["x"], height - item["y"]), (item["x"] + item["w"], height - item["y"] - item["h"])]
                pts = [(a * u + cc * v + e, b * u + d * v + f) for u, v in corners]
                rect = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
                if item["type"] == "field":
                    new_fields.append((n - 1, rect, item))
                    continue
                writer.add_annotation(page_number=n - 1, annotation=Link(rect=rect, url=item["url"], border=[0, 0, 0]))
        if new_fields:
            _add_fields(writer, new_fields, set((reader.get_fields() or {}).keys()))
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
