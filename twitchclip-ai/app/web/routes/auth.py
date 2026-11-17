"""Login und Logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.enums import ActivityCategory, ActivityLevel
from app.models import User
from app.redis_client import get_redis
from app.security import csrf
from app.security.ratelimit import LoginRateLimiter
from app.services.activity import log_activity
from app.services.users import authenticate
from app.web.deps import SESSION_USER, SESSION_VERSION, csrf_protect, get_current_user, safe_next
from app.web.templating import render

router = APIRouter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login")
def login_form(request: Request, next: str = "/", user: User | None = Depends(get_current_user)):
    if user is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "login.html", {"next": safe_next(next)})


@router.post("/login", dependencies=[Depends(csrf_protect)])
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    ip = client_ip(request)
    username = username.strip()[:64]
    limiter = LoginRateLimiter(get_redis(), settings.login_max_attempts, settings.login_window_seconds)
    context = {"next": safe_next(next), "username": username}

    if limiter.is_blocked(ip, username):
        log_activity(
            db,
            ActivityLevel.WARNING,
            ActivityCategory.AUTH,
            f"Login gesperrt (zu viele Fehlversuche) für '{username}'",
            details={"ip": ip},
            commit=True,
        )
        context["error"] = "Zu viele Fehlversuche. Bitte in einigen Minuten erneut versuchen."
        return render(request, "login.html", context, status_code=429)

    user = authenticate(db, username, password)
    if user is None:
        limiter.register_failure(ip, username)
        log_activity(
            db,
            ActivityLevel.WARNING,
            ActivityCategory.AUTH,
            f"Fehlgeschlagener Login für '{username}'",
            details={"ip": ip},
            commit=True,
        )
        context["error"] = "Benutzername oder Passwort ist falsch."
        return render(request, "login.html", context, status_code=401)

    limiter.reset(ip, username)
    # Session-Fixation vermeiden: Session vollständig neu aufbauen.
    request.session.clear()
    request.session[SESSION_USER] = user.id
    request.session[SESSION_VERSION] = user.session_version
    csrf.rotate_token(request)
    log_activity(db, ActivityLevel.INFO, ActivityCategory.AUTH, f"Login: {user.username}", user_id=user.id)
    db.commit()
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
