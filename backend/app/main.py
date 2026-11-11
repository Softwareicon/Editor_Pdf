"""PDF Master Pro API entry point: `uvicorn app.main:app` (run from the backend/ directory)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import __version__
from .config import get_settings
from .database import SessionLocal
from .errors import install_error_handlers
from .routers import auth, documents, edit, editor, pages
from .security.headers import SecurityHeadersMiddleware
from .security.tokens import purge_expired_refresh_tokens
from .services import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pdfmaster")


def _housekeeping() -> None:
    removed = storage.cleanup_temp()
    with SessionLocal() as db:
        purged = purge_expired_refresh_tokens(db)
    if removed or purged:
        logger.info("Housekeeping: removed %d temp files, %d expired sessions", removed, purged)


@asynccontextmanager
async def lifespan(_: FastAPI):
    async def _loop():
        while True:
            try:
                await asyncio.to_thread(_housekeeping)
            except Exception:
                logger.exception("Housekeeping failed")
            await asyncio.sleep(1800)

    task = asyncio.create_task(_loop())
    yield
    task.cancel()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="PDF Master Pro API", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    install_error_handlers(app)
    app.add_middleware(SecurityHeadersMiddleware, hsts=s.cookie_secure)
    if s.cors_origin_list:
        app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
                           expose_headers=["Content-Disposition"])

    app.include_router(auth.router)
    app.include_router(documents.router)
    app.include_router(editor.router)
    app.include_router(pages.router)
    app.include_router(edit.router)

    @app.get("/api/health", tags=["System"])
    def health():
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__, "max_upload_mb": s.max_upload_mb}

    if s.serve_frontend and s.frontend_dir.is_dir():
        # Convenience for local use and single-host deployments. Must be mounted last.
        app.mount("/", StaticFiles(directory=s.frontend_dir, html=True), name="frontend")
    return app


app = create_app()
