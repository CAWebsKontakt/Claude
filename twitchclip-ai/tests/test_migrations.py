"""Migrationen: Upgrade/Downgrade funktionieren und entsprechen exakt den Modellen."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.db import Base

ROOT = Path(__file__).resolve().parent.parent


def _config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["skip_logging_config"] = True
    return config


def test_upgrade_and_downgrade_sqlite(tmp_path, settings):
    url = f"sqlite+pysqlite:///{tmp_path / 'migrate.db'}"
    config = _config(url)
    command.upgrade(config, "head")
    engine = create_engine(url)
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="TEST_DATABASE_URL (PostgreSQL) nicht gesetzt",
)
def test_migrations_match_models_postgres(settings):
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    config = _config(url)
    command.upgrade(config, "head")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"Modelle und Migrationen weichen ab: {diff}"
    command.downgrade(config, "base")
    engine.dispose()
