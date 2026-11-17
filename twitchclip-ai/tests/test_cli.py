import io
import re

from app import cli
from app.models import Streamer, User


def test_generate_secrets(capsys):
    assert cli.main(["generate-secrets"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"^SECRET_KEY=\S{40,}$", out, re.M)
    assert re.search(r"^TOKEN_ENCRYPTION_KEY=\S{44}$", out, re.M)
    assert re.search(r"^TWITCH_EVENTSUB_SECRET=[0-9a-f]{64}$", out, re.M)


def test_create_user_and_reset_password(database, db_session, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("Ein-Gutes-Passwort-9\n"))
    assert cli.main(["create-user", "--username", "Chef", "--role", "admin", "--password-stdin"]) == 0
    user = db_session.query(User).one()
    assert user.username == "chef" and user.role == "admin"

    monkeypatch.setattr("sys.stdin", io.StringIO("kurz\n"))
    assert cli.main(["reset-password", "--username", "chef", "--password-stdin"]) == 1

    monkeypatch.setattr("sys.stdin", io.StringIO("Noch-Ein-Gutes-Passwort-1\n"))
    assert cli.main(["reset-password", "--username", "chef", "--password-stdin"]) == 0
    db_session.expire_all()
    assert db_session.query(User).one().session_version == 2


def test_add_and_list_streamers(database, db_session, capsys):
    assert cli.main(["add-streamer", "kanal_a", "https://twitch.tv/KanalB"]) == 0
    assert cli.main(["add-streamer", "kanal_a"]) == 1
    assert {s.twitch_login for s in db_session.query(Streamer).all()} == {"kanal_a", "kanalb"}
    cli.main(["list-streamers"])
    assert "Nur öffentliche Metadaten" in capsys.readouterr().out


def test_check_config(settings, capsys):
    assert cli.main(["check-config"]) == 0
    assert "Konfiguration gültig" in capsys.readouterr().out


def test_rotate_token_key(database, db_session):
    from app.models import OAuthToken

    db_session.add(
        OAuthToken(
            provider="twitch", external_user_id="1", scopes=[], access_token="abc", refresh_token="def"
        )
    )
    db_session.commit()
    assert cli.main(["rotate-token-key"]) == 0
    db_session.expire_all()
    assert db_session.query(OAuthToken).one().access_token == "abc"
