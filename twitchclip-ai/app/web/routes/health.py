"""Health-Checks für Docker, Caddy und externes Monitoring (ohne Login, ohne sensible Daten)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import __version__
from app.config import Settings, get_settings
from app.db import get_db
from app.redis_client import get_redis
from app.services.system import collect_status

router = APIRouter()


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    """Liveness: Prozess läuft und antwortet."""
    return {"status": "ok", "version": __version__}


@router.get("/readyz", include_in_schema=False)
def readyz(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> JSONResponse:
    """Readiness: Datenbank und Redis erreichbar."""
    status = collect_status(settings, db, get_redis(), detailed=False)
    body = {
        "status": "ok" if status.ready else "degraded",
        "database": "ok" if status.database_ok else "error",
        "redis": "ok" if status.redis_ok else "error",
        "version": __version__,
    }
    return JSONResponse(body, status_code=200 if status.ready else 503)
