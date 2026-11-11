# Development checklist

Legend: [x] implemented and covered by automated tests · [ ] not started. Nothing is half-done:
unfinished features have no buttons or endpoints.

## Phase 1 — Foundation (complete)
- [x] Repository structure, configuration from environment, `.env.example`, `.gitignore`
- [x] Database models for all tables + Alembic migration (verified on SQLite and PostgreSQL 16)
- [x] Register / login / logout / refresh / me — Argon2id, JWT access token, rotating refresh cookie
- [x] Role field and `require_role` dependency (no admin screens yet)
- [x] Upload with validation: size limit, PDF signature, structural parse in a sandboxed subprocess, page limit
- [x] List (paginated), details, download, delete (overwrite + unlink) — all scoped to the owner
- [x] Audit log for auth and document actions
- [x] Rate limiting on auth endpoints (in-process), secure headers + strict CSP, CORS allow-list, CSRF header check
- [x] Temporary-file and expired-session cleanup
- [x] Sign-in page, dashboard (drag-and-drop upload with progress, recent files, download, delete)
- [x] Viewer: lazy page rendering, thumbnails, outline, attachments, zoom in/out, fit page, fit width,
      page navigation, full-document search with highlights, download, print, open another PDF,
      password prompt for protected files (password stays in the browser)
- [x] Docker image + compose file, CI workflow, Windows setup/start scripts, README
- [x] Tests: 26 backend (pytest), 5 browser (Playwright)

## Phase 2 — Core editing (in progress)

Part A — page management and history (complete)
- [x] Pages: delete, rotate, reorder (drag thumbnails), reverse, duplicate, add blank, extract to a new document
- [x] Versions: every edit is stored as a new `document_versions` row; version list endpoint; history capped at 50
- [x] Undo / redo (buttons and Ctrl+Z / Ctrl+Y); a new edit after undo discards the undone versions and their files
- [x] Edits run in the sandboxed worker; page content is copied, never re-rendered
- [x] Encrypted PDFs (including owner-password-only) are refused rather than silently decrypted
- [x] Tests: 10 more backend tests (36 total), 2 more browser tests (7 total); upgrade of a Phase 1 database verified on PostgreSQL

Part B1 — overlay editing (complete)
- [x] Add text (font, size, colour, bold, italic; move, resize, re-edit); text the standard PDF fonts cannot
      encode (Hindi and other scripts) is saved as a picture of the text
- [x] Whiteout (opaque box that hides content so new text can be placed on top), highlight
- [x] Rectangle, ellipse, line, arrow, freehand pen
- [x] Insert image (PNG/JPEG/WebP/GIF), drawn signature (visual only)
- [x] Objects stay editable until "Save edits"; a save is one new version, so Undo/Redo work
- [x] Tests: 5 more backend tests (41 total), 1 more browser test (8 total)

Part B2 — not started
- [ ] **Java engine (PDFBox).** Blocked in the build environment: Maven Central was unreachable, so the
      engine could not be compiled or tested there. Page management therefore uses pypdf (BSD), which
      handles it well. See "Text editing engine" in ARCHITECTURE.md for the decision this forces.
- [ ] Editing the PDF's own text in place (existing text can currently only be covered and retyped)
- [ ] Content-type detection (native / scanned / outlined / flattened) surfaced in the UI
- [ ] Images: insert, replace, delete, resize, crop, rotate, move, opacity
- [ ] Pages still missing: insert pages from another PDF, replace pages, resize page, crop page
- [ ] Annotations: highlight, underline, strikethrough, freehand, comments, sticky notes, shapes, arrows, stamps
- [ ] Drawn / typed / image signatures (visual only — labelled as such)
- [ ] Right-hand properties panel, layer list

## Phase 3 — PDF tools
- [ ] Redis job queue + worker, `/api/jobs`, progress polling
- [ ] Merge, split (range / every N / bookmarks)
- [ ] Compression (three presets + custom), size report, lossy operations labelled as lossy
- [ ] Conversions PDF ⇄ Word / Excel / PowerPoint / images / TXT / HTML, with fidelity warnings
- [ ] Add password / permissions; remove protection **only** with the correct password; inspect encryption
- [ ] Settings page, chunked/resumable upload, HTTP range streaming for very large files

## Phase 4 — Advanced tools
- [ ] OCR (English, Hindi, extra language packs), confidence display, searchable PDF, batch OCR
- [ ] Certificate-based digital signatures; warning when an edit invalidates an existing signature
- [ ] True redaction with post-verification
- [ ] Metadata editor, bookmarks editor, attachments insert/extract, repair, compare
- [ ] Page numbers, header/footer, watermark, Bates numbering, form fill, form field creation, batch processing

## Phase 5 — Quality and security
- [ ] Admin dashboard, user profile management
- [ ] Container-level sandbox for workers (seccomp, no network), malformed-PDF fuzz corpus
- [ ] Redis-backed rate limiting for multi-process deployments
- [ ] Load/performance tests with large documents, cross-browser matrix (Firefox, Safari, Edge)

## Phase 6 — Deployment
- [ ] S3-compatible storage backend
- [ ] Production compose with reverse proxy + TLS, deploy workflow, backup and operations guide
