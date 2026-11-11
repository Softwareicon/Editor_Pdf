"""Stand-alone page-arrangement worker (runs sandboxed, see services/sandbox.py).

Usage: python page_ops.py <input.pdf> <output.pdf> <max_pages>     (arrangement JSON on stdin)

The arrangement lists the pages of the output document in order:
    {"pages": [{"src": 0, "rotate": 90}, {"blank": {"like": 0}}, {"blank": {"w": 612, "h": 792}}, ...]}
`src` is a zero-based page index of the input; `rotate` is a clockwise delta in degrees.
Page content (text, fonts, vector graphics, images) is copied as-is, never re-rendered.
"""
import json
import sys


def arrange(src_path: str, out_path: str, max_pages: int, spec: dict) -> dict:
    from pypdf import PdfReader, PdfWriter
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(src_path, strict=False)
        if reader.is_encrypted:
            # Rewriting would silently strip the encryption/permissions. Never done without credentials.
            return {"ok": False, "error": "encrypted"}
        entries = spec["pages"]
        count = len(reader.pages)
        if not entries or len(entries) > max_pages:
            return {"ok": False, "error": "bad_request", "detail": "invalid number of pages"}
        for e in entries:
            idx = e["blank"].get("like") if "blank" in e else e["src"]
            if idx is not None and not (isinstance(idx, int) and 0 <= idx < count):
                return {"ok": False, "error": "bad_request", "detail": "page out of range"}
            if e.get("rotate", 0) % 90:
                return {"ok": False, "error": "bad_request", "detail": "rotation must be a multiple of 90"}

        sources = [e.get("src") for e in entries]
        plain = not any("blank" in e for e in entries) and len(set(sources)) == len(sources)
        outline_kept = True
        if sources == list(range(count)):
            writer = PdfWriter(clone_from=reader)  # same pages, same order: keep the whole document structure
        elif plain:
            writer = PdfWriter()
            writer.append(reader, pages=sources, import_outline=True)  # keeps bookmarks of the pages that remain
        else:
            writer = PdfWriter()
            outline_kept = not reader.outline
            for e in entries:
                if "blank" in e:
                    like = e["blank"].get("like")
                    if like is not None:
                        ref = reader.pages[like]
                        w, h = float(ref.cropbox.width), float(ref.cropbox.height)
                        if int(ref.rotation or 0) % 180:
                            w, h = h, w
                    else:
                        w, h = float(e["blank"]["w"]), float(e["blank"]["h"])
                    writer.add_blank_page(width=w, height=h)
                else:
                    writer.add_page(reader.pages[e["src"]])
        if sources != list(range(count)) and reader.metadata:
            writer.add_metadata({k: v for k, v in reader.metadata.items() if isinstance(v, str)})
        for page, e in zip(writer.pages, entries):
            if e.get("rotate", 0) % 360:
                page.rotate(e["rotate"] % 360)
        with open(out_path, "wb") as f:
            writer.write(f)
        return {"ok": True, "page_count": len(entries), "outline_kept": outline_kept}
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, IndexError,
            RecursionError, OSError, AssertionError) as exc:
        return {"ok": False, "error": "failed", "detail": f"{type(exc).__name__}: {exc}"[:200]}
    except MemoryError:
        return {"ok": False, "error": "failed", "detail": "memory limit exceeded"}


if __name__ == "__main__":
    print(json.dumps(arrange(sys.argv[1], sys.argv[2], int(sys.argv[3]), json.loads(sys.stdin.read()))))
