"""Jinja2-Templates, Filter und eine render()-Hilfsfunktion mit gemeinsamem Kontext."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.responses import Response

from app import __version__
from app.config import get_settings
from app.enums import (
    CLIP_SOURCE_LABELS,
    CLIP_STATUS_LABELS,
    GRANTOR_LABELS,
    HIGHLIGHT_STATUS_LABELS,
    MODE_LABELS,
    PUBLISH_METHOD_LABELS,
    PUBLISH_STATUS_LABELS,
    ROLE_LABELS,
)
from app.security import csrf
from app.timeutil import humanize_delta, to_local
from app.web.flash import pop_flashes

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _localtime(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    local = to_local(value, get_settings().tz)
    return local.strftime(fmt) if local else "–"


def _localdate(value: datetime | None) -> str:
    return _localtime(value, "%d.%m.%Y")


def _isotime(value: str | None) -> str:
    """ISO-Zeitstempel aus Redis (Statusanzeigen) lokal formatieren."""
    if not value:
        return "–"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return f"{_localtime(parsed)} ({humanize_delta(parsed)})"


def _filesize(value: int | float | None) -> str:
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _seconds(value: float | int | None) -> str:
    if value is None:
        return "–"
    total = int(round(float(value)))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def score_tone(value: float | int | None) -> str:
    if value is None:
        return "neutral"
    value = float(value)
    if value >= 80:
        return "good"
    if value >= get_settings().min_highlight_score:
        return "ok"
    if value >= 40:
        return "warn"
    return "bad"


env = templates.env
env.filters["localtime"] = _localtime
env.filters["localdate"] = _localdate
env.filters["isotime"] = _isotime
env.filters["timeago"] = humanize_delta
env.filters["filesize"] = _filesize
env.filters["seconds"] = _seconds
env.filters["score_tone"] = score_tone
env.globals.update(
    CLIP_SOURCE_LABELS=CLIP_SOURCE_LABELS,
    CLIP_STATUS_LABELS=CLIP_STATUS_LABELS,
    MODE_LABELS=MODE_LABELS,
    ROLE_LABELS=ROLE_LABELS,
    HIGHLIGHT_STATUS_LABELS=HIGHLIGHT_STATUS_LABELS,
    PUBLISH_STATUS_LABELS=PUBLISH_STATUS_LABELS,
    PUBLISH_METHOD_LABELS=PUBLISH_METHOD_LABELS,
    GRANTOR_LABELS=GRANTOR_LABELS,
)


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> Response:
    settings = get_settings()
    ctx: dict[str, Any] = {
        "request": request,
        "current_user": getattr(request.state, "user", None),
        "csrf_token": csrf.get_token(request),
        "flashes": pop_flashes(request),
        "app_name": settings.app_name,
        "version": __version__,
        "path": request.url.path,
        "settings": settings,
        "test_mode": settings.test_mode,
    }
    if context:
        ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
