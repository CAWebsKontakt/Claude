"""Gemeinsame Test-Fixtures.

Standard: SQLite im Speicher + fakeredis – keine externen Dienste nötig.
Mit TEST_DATABASE_URL=postgresql+psycopg://… laufen dieselben Tests gegen PostgreSQL.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet

# Umgebung setzen, BEVOR Anwendungsmodule importiert werden.
TEST_FERNET_KEY = Fernet.generate_key().decode()
os.environ.update(
    {
        "TCAI_ENV_FILE": "",
        "APP_ENV": "test",
        "APP_BASE_URL": "https://testserver",
        "SECRET_KEY": "test-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz",
        "TOKEN_ENCRYPTION_KEY": TEST_FERNET_KEY,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6399/0",
        "SESSION_COOKIE_SECURE": "true",
        "LOG_LEVEL": "WARNING",
        "STREAMER_LIST": "",
        "TWITCH_CLIENT_ID": "",
        "TIKTOK_CLIENT_KEY": "",
    }
)

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings, get_settings, reset_settings_cache  # noqa: E402
from app.db import Base, Database, set_database  # noqa: E402
from app.redis_client import set_redis  # noqa: E402
from app.security.crypto import _cipher_for  # noqa: E402

DEFAULT_PASSWORD = "Sehr-Sicheres-Passwort-42"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Iterator[Settings]:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    reset_settings_cache()
    _cipher_for.cache_clear()
    yield get_settings()
    reset_settings_cache()
    _cipher_for.cache_clear()


@pytest.fixture
def database(settings) -> Iterator[Database]:
    url = os.environ.get("TEST_DATABASE_URL") or "sqlite+pysqlite:///:memory:"
    db = Database(url)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    set_database(db)
    yield db
    set_database(None)
    Base.metadata.drop_all(db.engine)
    db.dispose()


@pytest.fixture
def db_session(database):
    session = database.session()
    yield session
    session.close()


@pytest.fixture
def redis():
    client = fakeredis.FakeRedis()
    set_redis(client)
    yield client
    set_redis(None)


@pytest.fixture
def app(settings, database, redis):
    from app.main import create_app

    return create_app(settings)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def make_user(db_session):
    from app.services.users import create_user

    def _make(username: str = "admin", role: str = "admin", password: str = DEFAULT_PASSWORD):
        user = create_user(db_session, username, password, role)
        db_session.commit()
        return user

    return _make


def get_csrf(client: TestClient, path: str = "/login") -> str:
    response = client.get(path)
    match = CSRF_RE.search(response.text)
    assert match, f"Kein CSRF-Token auf {path} gefunden (Status {response.status_code})"
    return match.group(1)


def login(client: TestClient, username: str = "admin", password: str = DEFAULT_PASSWORD, next_url: str = "/"):
    token = get_csrf(client, "/login")
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token, "next": next_url},
        follow_redirects=False,
    )


def post_form(client: TestClient, path: str, data: dict | None = None, *, csrf_page: str = "/"):
    payload = dict(data or {})
    payload["csrf_token"] = get_csrf(client, csrf_page)
    return client.post(path, data=payload, follow_redirects=False)


@pytest.fixture
def admin_client(client, make_user):
    make_user("admin", "admin")
    response = login(client)
    assert response.status_code == 303
    return client


@pytest.fixture
def twitch_settings(settings, monkeypatch):
    """Konfiguration mit Twitch-Zugangsdaten und öffentlicher HTTPS-Domain (EventSub aktiv)."""
    from tests.twitch_payloads import EVENTSUB_SECRET

    monkeypatch.setenv("TWITCH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("TWITCH_EVENTSUB_SECRET", EVENTSUB_SECRET)
    monkeypatch.setenv("APP_BASE_URL", "https://clips.example.com")
    reset_settings_cache()
    return get_settings()


@pytest.fixture
def helix(twitch_settings, redis):
    """HelixClient mit aufgezeichneten (nicht ausgeführten) Wartezeiten."""
    from app.integrations.twitch.client import HelixClient

    sleeps: list[float] = []
    client = HelixClient(twitch_settings, redis, sleep=sleeps.append)
    client.sleeps = sleeps
    yield client
    client.close()
