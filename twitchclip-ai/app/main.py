"""FastAPI-Anwendung."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.config import Settings, get_settings
from app.logging_setup import configure_logging
from app.services.storage import ensure_directories
from app.web.deps import NotAuthenticatedError
from app.web.middleware import SecurityHeadersMiddleware
from app.web.routes import (
    activity,
    auth,
    dashboard,
    health,
    highlights,
    media,
    oauth,
    publishing,
    streamers,
    system,
    tiktok,
    users,
    webhooks,
)
from app.web.templating import render

logger = logging.getLogger("tcai.web")

STATIC_DIR = Path(__file__).parent / "web" / "static"


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return request.url.path.startswith(("/api/", "/webhooks/", "/healthz", "/readyz")) or (
        "application/json" in accept and "text/html" not in accept
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=settings.app_env != "development")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            ensure_directories(settings)
        except OSError as exc:
            logger.error("Datenverzeichnisse konnten nicht angelegt werden", extra={"error": str(exc)})
        logger.info("TwitchClip AI gestartet", extra={"version": __version__, "env": settings.app_env})
        yield

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Reihenfolge: zuletzt hinzugefügte Middleware läuft zuerst.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key.get_secret_value() or "dev-only-insecure-secret-key-change-me",
        session_cookie="tcai_session",
        max_age=settings.session_max_age_hours * 3600,
        same_site="lax",
        https_only=settings.session_cookie_secure,
    )
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for module in (
        health,
        auth,
        dashboard,
        streamers,
        highlights,
        publishing,
        tiktok,
        activity,
        users,
        system,
        media,
        oauth,
        webhooks,
    ):
        app.include_router(module.router)

    @app.exception_handler(NotAuthenticatedError)
    async def _not_authenticated(request: Request, exc: NotAuthenticatedError):
        if _wants_json(request):
            return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)
        return RedirectResponse(f"/login?next={exc.next_url}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if _wants_json(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        titles = {403: "Keine Berechtigung", 404: "Nicht gefunden", 405: "Methode nicht erlaubt"}
        return render(
            request,
            "errors/error.html",
            {"code": exc.status_code, "title": titles.get(exc.status_code, "Fehler"), "detail": exc.detail},
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        if _wants_json(request):
            return JSONResponse({"detail": "Ungültige Eingabe"}, status_code=422)
        return render(
            request,
            "errors/error.html",
            {"code": 422, "title": "Ungültige Eingabe", "detail": "Die Anfrage enthielt ungültige Werte."},
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("Unbehandelter Fehler", extra={"path": request.url.path})
        if _wants_json(request):
            return JSONResponse({"detail": "Interner Fehler"}, status_code=500)
        return render(
            request,
            "errors/error.html",
            {"code": 500, "title": "Interner Fehler", "detail": "Der Fehler wurde protokolliert."},
            status_code=500,
        )

    return app


# Start: uvicorn app.main:create_app --factory
