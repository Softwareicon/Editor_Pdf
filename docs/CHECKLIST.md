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

## Phase 2 — Core editing
- [ ] Java engine (PDFBox) service + JUnit tests
- [ ] Text: select / modify / delete / insert, font, size, colour, weight, alignment, move, rotate
- [ ] Content-type detection (native / scanned / outlined / flattened) surfaced in the UI
- [ ] Images: insert, replace, delete, resize, crop, rotate, move, opacity
- [ ] Pages: add blank, delete, duplicate, reorder, rotate, extract, insert from PDF, replace, reverse, resize, crop
- [ ] Annotations: highlight, underline, strikethrough, freehand, comments, sticky notes, shapes, arrows, stamps
- [ ] Drawn / typed / image signatures (visual only — labelled as such)
- [ ] Undo / redo, Save (new `document_versions` row), right-hand properties panel, layer list

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
