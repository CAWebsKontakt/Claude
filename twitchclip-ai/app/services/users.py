"""Benutzerverwaltung."""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import ActivityCategory, ActivityLevel, UserRole
from app.models import User
from app.security.passwords import (
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)
from app.services.activity import log_activity
from app.timeutil import utcnow

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,64}$")


class UserError(ValueError):
    pass


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def get_by_username(db: Session, username: str) -> User | None:
    return db.scalar(select(User).where(User.username == normalize_username(username)))


def count_active_admins(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count(User.id)).where(User.role == UserRole.ADMIN.value, User.is_active.is_(True))
        )
        or 0
    )


def create_user(
    db: Session,
    username: str,
    password: str,
    role: UserRole | str = UserRole.VIEWER,
    *,
    actor: User | None = None,
) -> User:
    username = normalize_username(username)
    if not USERNAME_RE.match(username):
        raise UserError("Benutzername: 3–64 Zeichen, erlaubt sind Buchstaben, Ziffern, _ . -")
    try:
        role = UserRole(role)
    except ValueError as exc:
        raise UserError("Unbekannte Rolle") from exc
    try:
        validate_password(password, username=username)
    except PasswordPolicyError as exc:
        raise UserError(str(exc)) from exc
    if get_by_username(db, username):
        raise UserError("Dieser Benutzername ist bereits vergeben.")
    user = User(username=username, password_hash=hash_password(password), role=role.value)
    db.add(user)
    db.flush()
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.USER,
        f"Benutzer '{username}' mit Rolle '{role.value}' angelegt",
        user_id=actor.id if actor else None,
    )
    return user


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = get_by_username(db, username)
    if user is None or not user.is_active:
        verify_password(None, password)  # konstante Laufzeit
        return None
    if not verify_password(user.password_hash, password):
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    user.last_login_at = utcnow()
    return user


def change_password(db: Session, user: User, new_password: str, *, actor: User | None = None) -> None:
    try:
        validate_password(new_password, username=user.username)
    except PasswordPolicyError as exc:
        raise UserError(str(exc)) from exc
    user.password_hash = hash_password(new_password)
    user.session_version += 1  # alle bestehenden Sessions abmelden
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.USER,
        f"Passwort von '{user.username}' geändert",
        user_id=(actor or user).id,
    )


def set_role(db: Session, user: User, role: UserRole | str, *, actor: User) -> None:
    try:
        role = UserRole(role)
    except ValueError as exc:
        raise UserError("Unbekannte Rolle") from exc
    if (
        user.role == UserRole.ADMIN
        and role != UserRole.ADMIN
        and user.is_active
        and count_active_admins(db) <= 1
    ):
        raise UserError("Der letzte aktive Administrator kann nicht herabgestuft werden.")
    old = user.role
    user.role = role.value
    user.session_version += 1
    log_activity(
        db,
        ActivityLevel.INFO,
        ActivityCategory.USER,
        f"Rolle von '{user.username}' geändert: {old} → {role.value}",
        user_id=actor.id,
    )


def set_active(db: Session, user: User, active: bool, *, actor: User) -> None:
    if not active and user.id == actor.id:
        raise UserError("Sie können sich nicht selbst deaktivieren.")
    if not active and user.role == UserRole.ADMIN and user.is_active and count_active_admins(db) <= 1:
        raise UserError("Der letzte aktive Administrator kann nicht deaktiviert werden.")
    user.is_active = active
    user.session_version += 1
    log_activity(
        db,
        ActivityLevel.WARNING if not active else ActivityLevel.INFO,
        ActivityCategory.USER,
        f"Benutzer '{user.username}' {'aktiviert' if active else 'deaktiviert'}",
        user_id=actor.id,
    )
