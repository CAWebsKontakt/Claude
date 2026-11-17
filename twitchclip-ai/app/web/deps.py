"""Gemeinsame FastAPI-Dependencies: Benutzer, Rollen, CSRF."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import csrf


class NotAuthenticatedError(Exception):
    def __init__(self, next_url: str = "/") -> None:
        self.next_url = next_url


SESSION_USER = "uid"
SESSION_VERSION = "sv"


@dataclass(frozen=True)
class CurrentUser:
    """Unveränderliche Kopie des angemeldeten Benutzers für Templates.

    Templates dürfen nicht auf ORM-Objekte angewiesen sein: Fehlerseiten werden erst nach
    dem Schließen/Rollback der DB-Session gerendert.
    """

    id: int
    username: str
    role: str
    is_admin: bool
    can_edit: bool
    last_login_at: datetime | None

    @classmethod
    def from_user(cls, user: User) -> CurrentUser:
        return cls(
            id=user.id,
            username=user.username,
            role=user.role,
            is_admin=user.is_admin,
            can_edit=user.can_edit,
            last_login_at=user.last_login_at,
        )


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get(SESSION_USER)
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active or user.session_version != request.session.get(SESSION_VERSION):
        request.session.pop(SESSION_USER, None)
        request.session.pop(SESSION_VERSION, None)
        return None
    request.state.user = CurrentUser.from_user(user)
    return user


def require_user(request: Request, user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        raise NotAuthenticatedError(quote(target, safe="/?=&"))
    return user


def require_editor(user: User = Depends(require_user)) -> User:
    if not user.can_edit:
        raise HTTPException(status_code=403, detail="Für diese Aktion sind Redakteursrechte nötig.")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Nur Administratoren dürfen diese Aktion ausführen.")
    return user


async def csrf_protect(request: Request) -> None:
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    token = request.headers.get(csrf.HEADER_NAME)
    if not token:
        form = await request.form()
        value = form.get(csrf.FORM_FIELD)
        token = value if isinstance(value, str) else None
    if not csrf.is_valid(request, token):
        raise HTTPException(
            status_code=403,
            detail="Sicherheitsprüfung (CSRF) fehlgeschlagen. Bitte Seite neu laden und erneut versuchen.",
        )


def safe_next(url: str | None) -> str:
    """Verhindert Open-Redirects: nur relative Pfade innerhalb der Anwendung."""
    if not url or not url.startswith("/") or url.startswith("//") or "\\" in url:
        return "/"
    return url
