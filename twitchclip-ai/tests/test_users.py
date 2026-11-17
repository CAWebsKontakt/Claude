import pytest

from app.models import User
from app.services.users import UserError, create_user, set_active, set_role
from tests.conftest import login, post_form


def test_admin_can_create_user(admin_client, db_session):
    response = post_form(
        admin_client,
        "/users",
        {"username": "Redakteur1", "password": "Ein-Gutes-Passwort-9", "role": "editor"},
        csrf_page="/users",
    )
    assert response.status_code == 303
    user = db_session.query(User).filter_by(username="redakteur1").one()
    assert user.role == "editor"


def test_duplicate_and_invalid_users_rejected(db_session):
    create_user(db_session, "anna", "Ein-Gutes-Passwort-9", "viewer")
    with pytest.raises(UserError, match="vergeben"):
        create_user(db_session, "ANNA", "Ein-Gutes-Passwort-9", "viewer")
    with pytest.raises(UserError, match="Benutzername"):
        create_user(db_session, "a b", "Ein-Gutes-Passwort-9", "viewer")
    with pytest.raises(UserError, match="Rolle"):
        create_user(db_session, "berta", "Ein-Gutes-Passwort-9", "superuser")


def test_non_admin_cannot_manage_users(client, make_user):
    make_user("admin", "admin")
    make_user("viewer1", "viewer")
    assert login(client, "viewer1").status_code == 303
    assert client.get("/users").status_code == 403
    response = post_form(
        client, "/users", {"username": "x123", "password": "Ein-Gutes-Passwort-9", "role": "admin"}
    )
    assert response.status_code == 403


def test_last_admin_protected(db_session, make_user):
    admin = make_user("admin", "admin")
    other = make_user("zweit", "admin")
    with pytest.raises(UserError, match="selbst"):
        set_active(db_session, admin, False, actor=admin)
    set_active(db_session, other, False, actor=admin)
    with pytest.raises(UserError, match="letzte"):
        set_role(db_session, admin, "editor", actor=admin)


def test_role_change_ends_sessions(db_session, make_user):
    admin = make_user("admin", "admin")
    editor = make_user("edi", "editor")
    before = editor.session_version
    set_role(db_session, editor, "viewer", actor=admin)
    assert editor.session_version == before + 1
