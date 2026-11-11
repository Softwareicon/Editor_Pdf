# PDF Master Pro

A browser-based PDF platform built in phases. **Phase 1 (foundation) and the first part of Phase 2
(page management with undo/redo) are complete and tested.** Text and image editing, annotations,
conversion, OCR and the other tools are designed (see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)) but **not built yet** —
[`docs/CHECKLIST.md`](docs/CHECKLIST.md) lists exactly what exists.

## What works today

- **Accounts** — register, sign in, sign out; Argon2id password hashing; short-lived JWT access
  tokens with a rotating, revocable refresh cookie.
- **Documents** — drag-and-drop upload with progress, list, download, delete. Every file is
  validated (size, PDF signature, structural parse in a sandboxed subprocess) and is visible only to
  its owner.
- **Viewer** — fast lazy rendering (PDF.js), page thumbnails, document outline, attachments, zoom
  in/out, fit page, fit width, page navigation, whole-document search with highlights, download,
  print. Password-protected PDFs open after you type the password, which never leaves the browser.
- **Page tools** — delete, rotate, duplicate, reorder by dragging thumbnails, reverse, add a blank
  page, extract pages into a new document. Every edit is applied to the real PDF on the server and
  saved as a version; **Undo/Redo** step through versions, even after you close the browser.
  Page content is copied as-is (no re-rendering, no quality loss). Limits: bookmarks are dropped when
  you duplicate pages or add blank pages; encrypted PDFs cannot be edited yet.
- **Operations** — Alembic migrations (SQLite or PostgreSQL), audit log, rate limiting, strict
  Content-Security-Policy, automatic temp-file cleanup, Docker image, CI workflow.

## Architecture

```mermaid
flowchart LR
  B[Browser · PDF.js] --> API[FastAPI]
  API --> DB[(PostgreSQL / SQLite)]
  API --> ST[(File storage)]
  API -.Phase 3.-> Q[(Redis)] -.-> W[Workers] -.Phase 2.-> J[Java · PDFBox]
```

Stack: Python 3.12+, FastAPI, SQLAlchemy 2, Alembic, pypdf · HTML/CSS/ES modules, PDF.js 4,
Bootstrap 5 (both vendored, no CDN) · PostgreSQL 16 or SQLite. Java 21 + PDFBox joins in Phase 2.

## Quick start on Windows

1. Install **Python 3.12 or newer** from <https://www.python.org/downloads/> and tick
   *"Add python.exe to PATH"*.
2. Double-click **`setup_windows.bat`** (creates `.venv`, installs dependencies, writes `.env` with
   a random secret, creates the database).
3. Double-click **`start_pdf_master.bat`**. The app opens at <http://127.0.0.1:8000>.

**Upgrading from the Phase 1 zip:** copy your `.env` and `storage/` folder into the new folder, then
run `start_pdf_master.bat` — it upgrades the database automatically.

Create an account on the first screen, then upload a PDF (try the files in `samples/`).

> These two scripts have not yet been run on a real Windows machine — if one fails, the manual
> steps below do the same thing.

## Manual start (Windows, macOS, Linux)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env            # then set PDFMASTER_SECRET_KEY (see the comment in the file)
cd backend
alembic upgrade head
uvicorn app.main:app --port 8000
```

Open <http://127.0.0.1:8000>. The API serves the frontend, so there is no separate frontend step.
API documentation: <http://127.0.0.1:8000/api/docs>.

Java, Redis and Tesseract are **not needed yet**; they become requirements in Phases 2–4 and their
installation steps will be added then.

## Configuration

All settings are environment variables (or lines in `.env`) — see [`.env.example`](.env.example).
The important ones:

| Variable | Default | Meaning |
|---|---|---|
| `PDFMASTER_SECRET_KEY` | random per run in development | JWT signing key. **Required (≥ 32 chars) in production.** |
| `PDFMASTER_DATABASE_URL` | SQLite in `storage/` | e.g. `postgresql+psycopg://user:pass@host:5432/pdfmaster` |
| `PDFMASTER_MAX_UPLOAD_MB` | 100 | upload size limit |
| `PDFMASTER_CORS_ORIGINS` | empty | origins of a separately hosted frontend |
| `PDFMASTER_COOKIE_SECURE` | false | set `true` behind HTTPS |

### PostgreSQL

```sql
CREATE USER pdfmaster WITH PASSWORD 'choose-a-password';
CREATE DATABASE pdfmaster OWNER pdfmaster;
```

Set `PDFMASTER_DATABASE_URL` and run `alembic upgrade head` in `backend/`.

## Docker

```bash
cp .env.example .env     # set PDFMASTER_SECRET_KEY and POSTGRES_PASSWORD
docker compose up --build
```

Starts PostgreSQL and the API on port 8000. (Not yet run end to end — see the verification report.)

## Tests

```bash
pip install -r backend/requirements-dev.txt
pytest                         # 36 backend tests
playwright install chromium    # once
pytest tests/frontend          # 7 browser tests against a live server
```

`python tests/pdf_factory.py` regenerates the sample PDFs in `samples/`. They are produced by this
project's own code and contain no third-party content.

## Putting it on GitHub

```bash
git init && git add . && git commit -m "PDF Master Pro"
git branch -M main
git remote add origin https://github.com/<you>/pdf-master-pro.git
git push -u origin main
```

`.gitignore` already excludes `.env`, databases and everything under `storage/`.
`.github/workflows/ci.yml` runs both test suites on every push.

## Production deployment

- Run the API on a server (VPS or container platform) behind an HTTPS reverse proxy (Caddy, nginx,
  Traefik). Set `PDFMASTER_ENVIRONMENT=production`, a strong `PDFMASTER_SECRET_KEY`,
  `PDFMASTER_COOKIE_SECURE=true`, and a PostgreSQL `PDFMASTER_DATABASE_URL`.
- Run **one** API process for now (the rate limiter is in-process).
- Mount persistent storage at `storage/` and back it up together with the database.
- **GitHub Pages can host only the static `frontend/` folder.** It cannot run Python, the database
  or any processing. To use it: set `apiBase` in `frontend/assets/js/config.js` to your API URL, set
  `PDFMASTER_CORS_ORIGINS` to the Pages origin, `PDFMASTER_COOKIE_SAMESITE=none` and
  `PDFMASTER_COOKIE_SECURE=true`. Serving the frontend from the API (the default) is simpler.

## Security notes

- Uploaded PDFs are treated as hostile: parsed only in a subprocess with memory, CPU and time limits.
- Passwords: Argon2id. PDF passwords are never stored or sent to the server in Phase 1.
- Users can reach only their own documents; other users' IDs return 404.
- The application will never brute-force or bypass a PDF password.
- Report vulnerabilities privately to the repository owner rather than in a public issue.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `python` not recognised | Reinstall Python with "Add to PATH", open a new terminal |
| Signed out after every restart | Set `PDFMASTER_SECRET_KEY` in `.env` (otherwise a new key is generated each run) |
| "Only PDF files can be uploaded" | The file is not a PDF even if it ends in `.pdf` |
| "The file is corrupted or is not a valid PDF" | The file starts like a PDF but cannot be parsed; repair is a Phase 4 feature |
| Port 8000 in use | `set PDFMASTER_PORT=8080` before `start_pdf_master.bat`, or `--port 8080` |
| Login fails from a separately hosted frontend | Check `PDFMASTER_CORS_ORIGINS`, `COOKIE_SAMESITE=none`, `COOKIE_SECURE=true`, and HTTPS on both sides |

## Third-party software

PDF.js (Apache-2.0), Bootstrap (MIT) and SortableJS (MIT) are vendored under `frontend/assets/vendor/` with their
licence files. Dependency licence analysis, including why PyMuPDF (AGPL) is not used, is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#f-dependencies-and-licences).
