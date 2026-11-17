from app.models import ActivityLog, User
from tests.conftest import DEFAULT_PASSWORD, get_csrf, login, post_form


def test_pages_require_login(client):
    for path in [
        "/",
        "/streamers",
        "/highlights",
        "/clips",
        "/queue",
        "/published",
        "/activity",
        "/system",
        "/account",
    ]:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next=")


def test_json_clients_get_401(client):
    response = client.get("/streamers", headers={"accept": "application/json"}, follow_redirects=False)
    assert response.status_code == 401


def test_login_success_sets_secure_session_cookie(client, make_user, db_session):
    make_user("admin", "admin")
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert "tcai_session=" in cookie and "httponly" in cookie.lower() and "secure" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    page = client.get("/")
    assert page.status_code == 200
    assert "Übersicht" in page.text
    assert db_session.query(User).one().last_login_at is not None


def test_login_wrong_password(client, make_user, db_session):
    make_user("admin", "admin")
    response = login(client, password="falsches-passwort-xyz")
    assert response.status_code == 401
    assert "falsch" in response.text
    entry = db_session.query(ActivityLog).filter(ActivityLog.category == "auth").one()
    assert entry.level == "warning"


def test_login_is_rate_limited(client, make_user, settings):
    make_user("admin", "admin")
    for _ in range(settings.login_max_attempts):
        assert login(client, password="falsches-passwort-xyz").status_code == 401
    # Danach auch mit richtigem Passwort gesperrt
    assert login(client).status_code == 429


def test_login_requires_csrf(client, make_user):
    make_user("admin", "admin")
    client.get("/login")
    response = client.post(
        "/login", data={"username": "admin", "password": DEFAULT_PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 403


def test_open_redirect_prevented(client, make_user):
    make_user("admin", "admin")
    response = login(client, next_url="//evil.example/steal")
    assert response.headers["location"] == "/"


def test_next_parameter_respected(client, make_user):
    make_user("admin", "admin")
    response = login(client, next_url="/streamers")
    assert response.headers["location"] == "/streamers"


def test_logout(admin_client):
    response = post_form(admin_client, "/logout")
    assert response.status_code == 303
    assert admin_client.get("/", follow_redirects=False).status_code == 303


def test_inactive_user_cannot_login(client, make_user, db_session):
    user = make_user("bob", "viewer")
    user.is_active = False
    db_session.commit()
    assert login(client, "bob").status_code == 401


def test_password_change_invalidates_other_sessions(app, make_user, db_session):
    from fastapi.testclient import TestClient

    make_user("admin", "admin")
    with (
        TestClient(app, base_url="https://testserver") as first,
        TestClient(app, base_url="https://testserver") as second,
    ):
        assert login(first).status_code == 303
        assert login(second).status_code == 303
        response = post_form(
            first,
            "/account/password",
            {
                "current_password": DEFAULT_PASSWORD,
                "new_password": "Neues-Passwort-2026!",
                "new_password_repeat": "Neues-Passwort-2026!",
            },
        )
        assert response.status_code == 303
        assert first.get("/", follow_redirects=False).status_code == 200  # eigene Sitzung bleibt
        assert second.get("/", follow_redirects=False).status_code == 303  # andere Sitzung abgemeldet


def test_security_headers_present(client):
    response = client.get("/login")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_csrf_token_rotates_on_login(client, make_user):
    make_user("admin", "admin")
    before = get_csrf(client, "/login")
    login(client)
    after = get_csrf(client, "/account")
    assert before != after
