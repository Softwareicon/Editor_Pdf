# Phase 2, part A verification report (page management, versions, undo/redo)

Environment: Linux, Python 3.13, Google Chrome (headless) driven by Playwright, PostgreSQL 16.

| Check | Result |
|---|---|
| `pytest` — 36 backend tests (26 from Phase 1 + 10 for pages, versions, undo/redo) | pass |
| Output PDFs re-read with pypdf: page order, text, rotation and page sizes are as requested | pass |
| Metadata survives edits; bookmarks survive rotate, delete, reorder, reverse, extract | pass |
| Encrypted PDFs, including owner-password-only files, are refused (never silently decrypted) | pass |
| Another user gets 404 on every page, undo, redo and versions endpoint | pass |
| Undone versions and their files are removed when a new edit is made; history cap enforced | pass |
| `pytest tests/frontend` — 7 browser tests, including rotate, multi-select delete, duplicate, add blank, drag reorder, reverse, extract, undo/redo, with downloaded files checked page by page | pass |
| No JavaScript errors or Content-Security-Policy violations during the page-tools flow | pass |
| PostgreSQL 16: upgrade of a Phase 1 database holding a document (0001 → 0002), then edit, undo, redo | pass |

## Not verified

- Everything listed as unverified in `VERIFICATION_PHASE1.md` still applies (Windows scripts, Docker, GitHub Actions, other browsers, real-world PDFs).
- Page edits on PDFs with form fields, digital signatures, tagged structure or very large page counts. An edit will invalidate an existing digital signature and the app does not warn about it yet.
- Drag reordering was tested with a mouse only, not touch.

## Known limitations

- Bookmarks are dropped when pages are duplicated or blank pages are added.
- Links that point to another page inside the document may not survive delete, reorder or extract.
- Each edit stores a full copy of the file (up to 50 versions per document), so storage grows with edits.
- No Java engine yet: Maven Central was unreachable from the build environment (see ARCHITECTURE.md).
