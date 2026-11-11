# Phase 1 verification report

Environment: Linux, Python 3.13, Google Chrome (headless) driven by Playwright, PostgreSQL 16.

| Check | Result |
|---|---|
| `pytest` — 26 backend tests (auth, uploads, isolation, validation, migrations, headers) | pass |
| `pytest tests/frontend` — 5 browser tests on a live server (sign-up, login errors, upload, viewer, search, zoom, download byte-equality, delete, password-protected PDF, cross-user access) | pass |
| Canvas pixel check: pages and thumbnails contain real rendered content; off-screen pages are not rendered | pass |
| No JavaScript errors or Content-Security-Policy violations during the full flow | pass |
| `alembic upgrade head` + `alembic check` on SQLite | pass |
| `alembic upgrade head` + `alembic check` + register/upload/refresh/delete flow on PostgreSQL 16 | pass |

## Not verified in this environment

- **Windows scripts** (`setup_windows.bat`, `start_pdf_master.bat`) were written on Linux and have not been run on Windows.
- **Docker image and `docker compose up`**: no Docker daemon was available. The same commands the image runs
  (`alembic upgrade head`, `uvicorn app.main:app`) were exercised directly, including against PostgreSQL.
- **GitHub Actions workflow** has not run on GitHub.
- **Browsers other than Chrome**; printing was not exercised by a test (the print dialog cannot be automated headlessly).
- **Real-world PDFs.** Tests use generated PDFs (plain, landscape, AES-256 encrypted, truncated, non-PDF).
  Large, scanned, CJK or malformed production documents have not been tried yet.

## Known limitations

- Search highlights the whole text run containing a match, not the exact characters.
- The viewer downloads the complete file before displaying it (range-request streaming is planned for Phase 3).
- Rate limiting is per process; run a single API process until the Redis limiter lands.
- Overwrite-on-delete is best effort on SSDs and copy-on-write file systems; use disk encryption.
- There is no admin user interface and no way to create an admin other than editing the database.
