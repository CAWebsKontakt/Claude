from redis.exceptions import ConnectionError as RedisConnectionError

from tests.conftest import post_form


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_ok(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "database": "ok",
        "redis": "ok",
        "version": response.json()["version"],
    }


def test_readyz_reports_redis_failure(client, redis, monkeypatch):
    def broken_ping():
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "ping", broken_ping)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["redis"] == "error"


def test_system_page_shows_warnings_without_workers(admin_client):
    page = admin_client.get("/system")
    assert page.status_code == 200
    assert "Kein RQ-Worker aktiv" in page.text
    assert "Scheduler sendet keinen Herzschlag" in page.text
    assert "TOKEN_ENCRYPTION_KEY" not in page.text  # Geheimnisse nie anzeigen


def test_manual_cleanup_enqueues_job(admin_client, redis):
    from app.workers.queues import QUEUE_DEFAULT, get_queue

    response = post_form(admin_client, "/system/cleanup", csrf_page="/system")
    assert response.status_code == 303
    assert get_queue(QUEUE_DEFAULT, redis).count == 1
    post_form(admin_client, "/system/cleanup", csrf_page="/system")
    assert get_queue(QUEUE_DEFAULT, redis).count == 1  # keine Doppelverarbeitung
