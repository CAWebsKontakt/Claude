from datetime import timedelta

import pytest

from app.enums import RightsStatus
from app.models import ActivityLog, OAuthToken, Streamer, StreamerPermission
from app.services.permissions import PermissionError_, PermissionInput, add_permission, revoke_permission
from app.services.streamers import (
    StreamerError,
    add_streamer,
    compute_rights,
    normalize_login,
    seed_from_list,
    set_mode,
)
from app.timeutil import utcnow
from tests.conftest import login, post_form


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("KanalName", "kanalname"),
        ("@kanal_name", "kanal_name"),
        ("https://www.twitch.tv/SomeOne/videos", "someone"),
        ("twitch.tv/abc", "abc"),
    ],
)
def test_normalize_login(raw, expected):
    assert normalize_login(raw) == expected


@pytest.mark.parametrize("raw", ["", "a", "_bad", "name with space", "x" * 30, "https://twitch.tv/"])
def test_normalize_login_rejects_invalid(raw):
    with pytest.raises(StreamerError):
        normalize_login(raw)


def test_add_streamer_via_dashboard(admin_client, db_session):
    response = post_form(
        admin_client, "/streamers", {"login": "https://twitch.tv/TestKanal"}, csrf_page="/streamers"
    )
    assert response.status_code == 303
    streamer = db_session.query(Streamer).one()
    assert streamer.twitch_login == "testkanal"
    assert streamer.mode == "public"
    page = admin_client.get(response.headers["location"])
    assert "testkanal" in page.text
    assert "Nur öffentliche Metadaten" in page.text


def test_duplicate_streamer_shows_error(admin_client, db_session):
    post_form(admin_client, "/streamers", {"login": "kanal1"})
    response = post_form(admin_client, "/streamers", {"login": "KANAL1"})
    assert response.headers["location"] == "/streamers"
    assert "bereits vorhanden" in admin_client.get("/streamers").text
    assert db_session.query(Streamer).count() == 1


def test_viewer_cannot_add_streamer(client, make_user, db_session):
    make_user("admin", "admin")
    make_user("viewer1", "viewer")
    login(client, "viewer1")
    response = post_form(client, "/streamers", {"login": "kanal1"})
    assert response.status_code == 403
    assert db_session.query(Streamer).count() == 0


def test_seed_from_list_is_idempotent(db_session):
    assert seed_from_list(db_session, ["alpha", "beta", "kein gültiger name"]) == ["alpha", "beta"]
    assert seed_from_list(db_session, ["alpha", "gamma"]) == ["gamma"]
    assert db_session.query(Streamer).count() == 3


def _permission(**overrides):
    data = {
        "granted_by_name": "Max Streamer",
        "granted_by_role": "broadcaster",
        "evidence_reference": "Vertrag vom 01.11.2026, Ordner Verträge",
        "allows_processing": True,
        "allows_publishing": False,
        "confirmed": True,
    }
    data.update(overrides)
    return PermissionInput(**data)


def test_rights_flow_from_public_to_publishing(db_session, make_user, settings):
    admin = make_user()
    streamer = add_streamer(db_session, "kanal1", actor=admin)
    assert compute_rights(streamer).status == RightsStatus.PUBLIC_ONLY

    # Modus B ohne Erlaubnis nicht möglich
    with pytest.raises(StreamerError, match="Erlaubnis"):
        set_mode(db_session, streamer, "authorized", actor=admin)

    add_permission(db_session, streamer, _permission(), actor=admin, tz=settings.tz)
    set_mode(db_session, streamer, "authorized", actor=admin)
    rights = compute_rights(streamer)
    assert rights.status == RightsStatus.MISSING_OAUTH
    assert not rights.can_process

    db_session.add(
        OAuthToken(
            provider="twitch",
            streamer_id=streamer.id,
            external_user_id="42",
            scopes=["user:read:email"],
            access_token="a",
        )
    )
    db_session.flush()
    db_session.refresh(streamer)
    assert compute_rights(streamer).status == RightsStatus.MISSING_OAUTH  # falscher Scope

    token = streamer.oauth_tokens[0]
    token.scopes = ["channel:manage:clips"]
    db_session.flush()
    rights = compute_rights(streamer)
    assert rights.status == RightsStatus.READY_PROCESSING
    assert rights.can_process and not rights.can_publish

    add_permission(db_session, streamer, _permission(allows_publishing=True), actor=admin, tz=settings.tz)
    rights = compute_rights(streamer)
    assert rights.status == RightsStatus.READY_PUBLISHING
    assert rights.can_publish

    token.revoked_at = utcnow()
    db_session.flush()
    assert compute_rights(streamer).status == RightsStatus.MISSING_OAUTH


def test_expired_permission_blocks_processing(db_session, make_user, settings):
    admin = make_user()
    streamer = add_streamer(db_session, "kanal2", actor=admin)
    permission = add_permission(db_session, streamer, _permission(), actor=admin, tz=settings.tz)
    set_mode(db_session, streamer, "authorized", actor=admin)
    db_session.add(
        OAuthToken(
            provider="twitch",
            streamer_id=streamer.id,
            external_user_id="7",
            scopes=["editor:manage:clips"],
            access_token="a",
        )
    )
    db_session.flush()
    db_session.refresh(streamer)
    assert compute_rights(streamer).can_process
    permission.valid_until = utcnow() - timedelta(minutes=1)
    db_session.flush()
    assert compute_rights(streamer).status == RightsStatus.MISSING_PERMISSION


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"granted_by_name": ""}, "erteilt"),
        ({"evidence_reference": "x"}, "Nachweis"),
        ({"confirmed": False}, "bestätigen"),
        ({"allows_processing": False, "allows_publishing": True}, "Verarbeitung"),
        ({"granted_by_role": "chef"}, "Rolle"),
    ],
)
def test_permission_validation(db_session, make_user, settings, overrides, message):
    admin = make_user()
    streamer = add_streamer(db_session, "kanal3", actor=admin)
    with pytest.raises(PermissionError_, match=message):
        add_permission(db_session, streamer, _permission(**overrides), actor=admin, tz=settings.tz)


def test_revoking_last_permission_downgrades_to_public(db_session, make_user, settings):
    admin = make_user()
    streamer = add_streamer(db_session, "kanal4", actor=admin)
    permission = add_permission(db_session, streamer, _permission(), actor=admin, tz=settings.tz)
    set_mode(db_session, streamer, "authorized", actor=admin)
    assert revoke_permission(db_session, streamer, permission, "Zusammenarbeit beendet", actor=admin) is True
    assert streamer.mode == "public"
    with pytest.raises(PermissionError_, match="bereits"):
        revoke_permission(db_session, streamer, permission, "doppelt", actor=admin)


def test_permission_form_and_mode_switch_via_dashboard(admin_client, db_session):
    post_form(admin_client, "/streamers", {"login": "kanal5"})
    streamer = db_session.query(Streamer).one()
    response = post_form(
        admin_client,
        f"/streamers/{streamer.id}/permissions",
        {
            "granted_by_name": "Kanal Fünf",
            "granted_by_role": "broadcaster",
            "evidence_reference": "E-Mail vom 10.11.2026",
            "allows_processing": "1",
            "allows_publishing": "1",
            "confirmed": "1",
            "switch_to_authorized": "1",
            "valid_from": "2026-01-01",
            "valid_until": "",
        },
    )
    assert response.status_code == 303
    db_session.expire_all()
    streamer = db_session.get(Streamer, streamer.id)
    assert streamer.mode == "authorized"
    assert db_session.query(StreamerPermission).count() == 1
    page = admin_client.get(f"/streamers/{streamer.id}").text
    assert "Twitch-Autorisierung fehlt" in page

    permission = db_session.query(StreamerPermission).one()
    response = post_form(
        admin_client, f"/streamers/{streamer.id}/permissions/{permission.id}/revoke", {"reason": "Test"}
    )
    assert response.status_code == 303
    db_session.expire_all()
    assert db_session.get(Streamer, streamer.id).mode == "public"


def test_permission_without_confirmation_rejected_via_dashboard(admin_client, db_session):
    post_form(admin_client, "/streamers", {"login": "kanal6"})
    streamer = db_session.query(Streamer).one()
    post_form(
        admin_client,
        f"/streamers/{streamer.id}/permissions",
        {
            "granted_by_name": "X Y",
            "granted_by_role": "broadcaster",
            "evidence_reference": "Vertrag 123",
            "allows_processing": "1",
            "switch_to_authorized": "1",
        },
    )
    db_session.expire_all()
    assert db_session.query(StreamerPermission).count() == 0
    assert db_session.get(Streamer, streamer.id).mode == "public"


def test_delete_requires_exact_login(admin_client, db_session):
    post_form(admin_client, "/streamers", {"login": "kanal7"})
    streamer = db_session.query(Streamer).one()
    post_form(admin_client, f"/streamers/{streamer.id}/delete", {"confirm_login": "falsch"})
    assert db_session.query(Streamer).count() == 1
    response = post_form(admin_client, f"/streamers/{streamer.id}/delete", {"confirm_login": "KANAL7"})
    assert response.headers["location"] == "/streamers"
    db_session.expire_all()
    assert db_session.query(Streamer).count() == 0
    assert db_session.query(ActivityLog).filter(ActivityLog.message.contains("entfernt")).count() == 1


def test_toggle_active(admin_client, db_session):
    post_form(admin_client, "/streamers", {"login": "kanal8"})
    streamer = db_session.query(Streamer).one()
    post_form(admin_client, f"/streamers/{streamer.id}/active", {"active": "0"})
    db_session.expire_all()
    assert db_session.get(Streamer, streamer.id).is_active is False


def test_unknown_streamer_404(admin_client):
    assert admin_client.get("/streamers/9999").status_code == 404
