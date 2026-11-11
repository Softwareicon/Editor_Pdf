"""Stand-alone PDF inspection worker.

Run as a separate process (see services/pdf_inspect.py) so that a malicious or malformed
PDF can at worst crash or exhaust this short-lived, resource-limited process — never the API.
Imports only the standard library and pypdf. Prints a single JSON object to stdout.

Usage: python inspect_pdf.py <file> <max_pages>
"""
import json
import sys


def _clean(value, limit=500):
    text = str(value).replace("\x00", "")
    return text[:limit]


def inspect(path: str, max_pages: int) -> dict:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(path, strict=False)
        result = {"ok": True, "pdf_version": reader.pdf_header.replace("%PDF-", "")[:8],
                  "encrypted": bool(reader.is_encrypted), "needs_password": False,
                  "page_count": None, "metadata": {}, "pages": []}
        if reader.is_encrypted and not reader.decrypt(""):
            # Encrypted with a user password we do not have. Never guessed or bypassed.
            result["needs_password"] = True
            return result
        count = len(reader.pages)
        if count > max_pages:
            return {"ok": False, "error": "too_many_pages", "detail": f"{count} pages (limit {max_pages})"}
        if count == 0:
            return {"ok": False, "error": "corrupted", "detail": "document has no pages"}
        result["page_count"] = count
        meta = reader.metadata or {}
        for key in ("/Title", "/Author", "/Subject", "/Keywords", "/Creator", "/Producer",
                    "/CreationDate", "/ModDate"):
            if meta.get(key) is not None:
                result["metadata"][key.lstrip("/").lower()] = _clean(meta.get(key))
        for i, page in enumerate(reader.pages):
            box = page.cropbox
            result["pages"].append({"index": i + 1, "width": round(float(box.width), 2),
                                    "height": round(float(box.height), 2),
                                    "rotation": int(page.rotation or 0) % 360})
        return result
    except (PyPdfError, ValueError, KeyError, TypeError, AttributeError, IndexError,
            RecursionError, OSError, AssertionError) as exc:
        return {"ok": False, "error": "corrupted", "detail": _clean(f"{type(exc).__name__}: {exc}", 200)}
    except MemoryError:
        return {"ok": False, "error": "resource_limit", "detail": "memory limit exceeded"}


if __name__ == "__main__":
    print(json.dumps(inspect(sys.argv[1], int(sys.argv[2]))))
