"""CSRF-Schutz nach dem Synchronizer-Token-Muster (Token in der signierten Session)."""

from __future__ import annotations

import hmac
import secrets

from starlette.requests import Request

SESSION_KEY = "csrf"
FORM_FIELD = "csrf_token"
HEADER_NAME = "x-csrf-token"


def get_token(request: Request) -> str:
    token = request.session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


def rotate_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session[SESSION_KEY] = token
    return token


def is_valid(request: Request, submitted: str | None) -> bool:
    expected = request.session.get(SESSION_KEY)
    if not expected or not submitted:
        return False
    return hmac.compare_digest(str(expected), str(submitted))
