"""Kommandozeile für Betrieb und Einrichtung.

Beispiele (im Container):
    python -m app.cli generate-secrets
    python -m app.cli bootstrap
    python -m app.cli create-user --username admin --role admin
    python -m app.cli reset-password --username admin
    python -m app.cli add-streamer somechannel
    python -m app.cli check-config
    python -m app.cli cleanup-storage --dry-run
    python -m app.cli rotate-token-key
    python -m app.cli make-samples --count 3 --duration 60
    python -m app.cli ingest-samples
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _print_err(message: str) -> None:
    print(message, file=sys.stderr)


def cmd_generate_secrets(_args: argparse.Namespace) -> int:
    from cryptography.fernet import Fernet

    print("# In die .env-Datei übernehmen (niemals in Git einchecken!)")
    print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"TOKEN_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
    print(f"TWITCH_EVENTSUB_SECRET={secrets.token_hex(32)}")
    print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(24).replace('-', 'x').replace('_', 'y')}")
    print(f"REDIS_PASSWORD={secrets.token_urlsafe(24).replace('-', 'x').replace('_', 'y')}")
    return 0


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(config, "head")


def cmd_bootstrap(_args: argparse.Namespace) -> int:
    """Migrationen ausführen, Verzeichnisse anlegen, STREAMER_LIST importieren."""
    from app.config import get_settings
    from app.db import session_scope
    from app.logging_setup import configure_logging
    from app.services.storage import ensure_directories
    from app.services.streamers import seed_from_list

    settings = get_settings()
    configure_logging(settings.log_level)
    print("→ Datenbank-Migrationen …")
    run_migrations()
    print("→ Datenverzeichnisse …")
    ensure_directories(settings)
    if settings.streamer_list:
        with session_scope() as db:
            added = seed_from_list(db, settings.streamer_list)
        print(f"→ STREAMER_LIST: {len(added)} neu angelegt ({', '.join(added) or 'keine'})")
    from sqlalchemy import func, select

    from app.models import User

    with session_scope() as db:
        users = db.scalar(select(func.count(User.id))) or 0
    if not users:
        print("! Noch kein Benutzer vorhanden. Anlegen mit:")
        print("  docker compose exec web python -m app.cli create-user --username admin --role admin")
    print("✓ Bootstrap abgeschlossen")
    return 0


def _read_password(args: argparse.Namespace) -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Passwort (mind. 12 Zeichen): ")
    second = getpass.getpass("Passwort wiederholen: ")
    if first != second:
        raise SystemExit("Die Passwörter stimmen nicht überein.")
    return first


def cmd_create_user(args: argparse.Namespace) -> int:
    from app.db import session_scope
    from app.services.users import UserError, create_user

    password = _read_password(args)
    try:
        with session_scope() as db:
            user = create_user(db, args.username, password, args.role)
            print(f"✓ Benutzer '{user.username}' ({user.role}) angelegt")
    except UserError as exc:
        _print_err(f"Fehler: {exc}")
        return 1
    return 0


def cmd_reset_password(args: argparse.Namespace) -> int:
    from app.db import session_scope
    from app.services.users import UserError, change_password, get_by_username

    password = _read_password(args)
    try:
        with session_scope() as db:
            user = get_by_username(db, args.username)
            if user is None:
                _print_err(f"Benutzer '{args.username}' nicht gefunden")
                return 1
            change_password(db, user, password)
            user.is_active = True
            print(f"✓ Passwort für '{user.username}' gesetzt, alle Sitzungen beendet")
    except UserError as exc:
        _print_err(f"Fehler: {exc}")
        return 1
    return 0


def cmd_add_streamer(args: argparse.Namespace) -> int:
    from app.db import session_scope
    from app.services.streamers import StreamerError, add_streamer

    exit_code = 0
    for login in args.logins:
        try:
            with session_scope() as db:
                streamer = add_streamer(db, login, notes="Per CLI hinzugefügt")
                print(f"✓ {streamer.twitch_login} hinzugefügt (Modus A)")
        except StreamerError as exc:
            _print_err(f"✗ {exc}")
            exit_code = 1
    return exit_code


def cmd_list_streamers(_args: argparse.Namespace) -> int:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.db import session_scope
    from app.models import Streamer
    from app.services.streamers import compute_rights

    with session_scope() as db:
        rows = db.scalars(
            select(Streamer)
            .options(selectinload(Streamer.permissions), selectinload(Streamer.oauth_tokens))
            .order_by(Streamer.twitch_login)
        ).all()
        for s in rows:
            rights = compute_rights(s)
            live = "LIVE" if s.is_live else "offline"
            active = "aktiv" if s.is_active else "pausiert"
            print(f"{s.id:>4}  {s.twitch_login:<26} {s.mode:<11} {live:<8} {active:<9} {rights.label}")
    return 0


def cmd_check_config(_args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    from app.config import Settings

    try:
        settings = Settings()
    except ValidationError as exc:
        _print_err("Konfiguration ungültig:")
        for error in exc.errors():
            loc = ".".join(str(x) for x in error["loc"]) or "(allgemein)"
            _print_err(f"  - {loc}: {error['msg']}")
        return 1
    print(json.dumps(settings.public_summary(), indent=2, ensure_ascii=False))
    warnings = []
    if not settings.twitch_configured:
        warnings.append("Twitch-API nicht konfiguriert (ab Phase 2 nötig).")
    if not settings.tiktok_configured:
        warnings.append("TikTok-API nicht konfiguriert (ab Phase 6 nötig).")
    for warning in warnings:
        print(f"Hinweis: {warning}")
    print("✓ Konfiguration gültig")
    return 0


def cmd_cleanup_storage(args: argparse.Namespace) -> int:
    from app.workers.jobs.maintenance import cleanup_storage_job

    result = cleanup_storage_job(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if not result["errors"] else 2


def cmd_rotate_token_key(_args: argparse.Namespace) -> int:
    """Verschlüsselt alle gespeicherten Tokens mit dem ersten Schlüssel aus TOKEN_ENCRYPTION_KEY neu."""
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from app.db import session_scope
    from app.models import OAuthToken

    count = 0
    with session_scope() as db:
        for token in db.scalars(select(OAuthToken)).all():
            # Lesen entschlüsselt mit beliebigem Schlüssel, Schreiben verschlüsselt mit dem ersten.
            token.access_token = str(token.access_token)
            if token.refresh_token is not None:
                token.refresh_token = str(token.refresh_token)
            flag_modified(token, "access_token")
            flag_modified(token, "refresh_token")
            count += 1
    print(f"✓ {count} Token(s) neu verschlüsselt")
    return 0


def cmd_make_samples(args: argparse.Namespace) -> int:
    """Synthetische Beispielvideos für den Testmodus erzeugen (keine Fremdinhalte)."""
    from app.config import get_settings
    from app.services.samples import SampleGenerationError, generate_samples

    try:
        created = generate_samples(
            get_settings(), count=args.count, duration=args.duration, overwrite=args.overwrite
        )
    except SampleGenerationError as exc:
        _print_err(f"Fehler: {exc}")
        return 1
    for path in created:
        print(f"✓ {path}")
    if not created:
        print("Keine neuen Dateien (bereits vorhanden – mit --overwrite neu erzeugen).")
    return 0


def cmd_ingest_samples(_args: argparse.Namespace) -> int:
    """Beispieldateien sofort übernehmen (nur mit TEST_MODE=true)."""
    from app.config import get_settings
    from app.db import session_scope
    from app.services.clip_processing import ingest_samples

    settings = get_settings()
    if not settings.test_mode:
        _print_err("TEST_MODE ist nicht aktiv – in der .env TEST_MODE=true setzen und neu starten.")
        return 1
    with session_scope() as db:
        created = ingest_samples(db, settings)
        print(f"✓ {len(created)} Beispieldatei(en) übernommen")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="TwitchClip AI – Verwaltung")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("generate-secrets", help="Sichere Zufallswerte für .env erzeugen").set_defaults(
        func=cmd_generate_secrets
    )
    sub.add_parser("bootstrap", help="Migrationen + Verzeichnisse + STREAMER_LIST").set_defaults(
        func=cmd_bootstrap
    )

    p = sub.add_parser("create-user", help="Benutzer anlegen")
    p.add_argument("--username", required=True)
    p.add_argument("--role", choices=["admin", "editor", "viewer"], default="viewer")
    p.add_argument("--password-stdin", action="store_true", help="Passwort von stdin lesen")
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("reset-password", help="Passwort zurücksetzen")
    p.add_argument("--username", required=True)
    p.add_argument("--password-stdin", action="store_true")
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser("add-streamer", help="Streamer hinzufügen (Modus A)")
    p.add_argument("logins", nargs="+")
    p.set_defaults(func=cmd_add_streamer)

    sub.add_parser("list-streamers", help="Streamer auflisten").set_defaults(func=cmd_list_streamers)
    sub.add_parser("check-config", help="Konfiguration prüfen").set_defaults(func=cmd_check_config)

    p = sub.add_parser("cleanup-storage", help="Alte Videodateien löschen")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_cleanup_storage)

    p = sub.add_parser("make-samples", help="Synthetische Beispielvideos für den Testmodus erzeugen")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--duration", type=int, default=60, help="Sekunden je Video (10–600)")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_make_samples)

    sub.add_parser("ingest-samples", help="Beispieldateien übernehmen (TEST_MODE=true)").set_defaults(
        func=cmd_ingest_samples
    )

    sub.add_parser("rotate-token-key", help="Tokens mit neuem Schlüssel verschlüsseln").set_defaults(
        func=cmd_rotate_token_key
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
