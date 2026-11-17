"""Benutzerverwaltung (nur Administratoren) und eigenes Konto."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.enums import UserRole
from app.models import User
from app.security.passwords import verify_password
from app.services import users as user_service
from app.services.users import UserError
from app.web.deps import SESSION_VERSION, csrf_protect, require_admin, require_user
from app.web.flash import flash
from app.web.templating import render

router = APIRouter()


def _get_user(db: Session, user_id: int) -> User:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Benutzer nicht gefunden")
    return target


@router.get("/users")
def list_users(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.username)).all()
    return render(request, "users/list.html", {"users": users, "roles": list(UserRole), "nav": "users"})


@router.post("/users", dependencies=[Depends(csrf_protect)])
def create_user(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    role: str = Form(UserRole.VIEWER.value),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        created = user_service.create_user(db, username, password, role, actor=admin)
        db.commit()
        flash(request, f"Benutzer '{created.username}' angelegt.", "success")
    except UserError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/role", dependencies=[Depends(csrf_protect)])
def change_role(
    request: Request,
    user_id: int,
    role: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = _get_user(db, user_id)
    try:
        user_service.set_role(db, target, role, actor=admin)
        db.commit()
        if target.id == admin.id:
            request.session[SESSION_VERSION] = target.session_version
        flash(request, "Rolle geändert.", "success")
    except UserError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/active", dependencies=[Depends(csrf_protect)])
def change_active(
    request: Request,
    user_id: int,
    active: str = Form("0"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = _get_user(db, user_id)
    try:
        user_service.set_active(db, target, active == "1", actor=admin)
        db.commit()
        flash(request, "Status geändert.", "success")
    except UserError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/password", dependencies=[Depends(csrf_protect)])
def reset_password(
    request: Request,
    user_id: int,
    password: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = _get_user(db, user_id)
    try:
        user_service.change_password(db, target, password, actor=admin)
        db.commit()
        if target.id == admin.id:
            request.session[SESSION_VERSION] = target.session_version
        flash(
            request,
            f"Passwort für '{target.username}' gesetzt. Bestehende Sitzungen wurden beendet.",
            "success",
        )
    except UserError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse("/users", status_code=303)


@router.get("/account")
def account(request: Request, user: User = Depends(require_user)):
    return render(request, "account.html", {"nav": "account"})


@router.post("/account/password", dependencies=[Depends(csrf_protect)])
def change_own_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password_repeat: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not verify_password(user.password_hash, current_password):
        flash(request, "Das aktuelle Passwort ist falsch.", "error")
        return RedirectResponse("/account", status_code=303)
    if new_password != new_password_repeat:
        flash(request, "Die neuen Passwörter stimmen nicht überein.", "error")
        return RedirectResponse("/account", status_code=303)
    try:
        user_service.change_password(db, user, new_password)
        db.commit()
        # Diese Sitzung bleibt gültig, alle anderen werden abgemeldet.
        request.session[SESSION_VERSION] = user.session_version
        flash(request, "Passwort geändert. Andere Sitzungen wurden abgemeldet.", "success")
    except UserError as exc:
        db.rollback()
        flash(request, str(exc), "error")
    return RedirectResponse("/account", status_code=303)
