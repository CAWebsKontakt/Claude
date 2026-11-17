"""Einmalige Hinweise (Flash-Messages) über die Session."""

from __future__ import annotations

from starlette.requests import Request

_KEY = "_flashes"
MAX_FLASHES = 5


def flash(request: Request, message: str, category: str = "info") -> None:
    flashes = list(request.session.get(_KEY, []))
    flashes.append([category, message[:500]])
    request.session[_KEY] = flashes[-MAX_FLASHES:]


def pop_flashes(request: Request) -> list[tuple[str, str]]:
    flashes = request.session.pop(_KEY, [])
    return [(c, m) for c, m in flashes]
