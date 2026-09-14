from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.config import get_settings

ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


def _recreate_database(url: str) -> None:
    parsed = make_url(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def engine() -> Engine:
    """Fresh test database, migrated from zero, with a downgrade round-trip."""
    url = get_settings().test_database_url
    try:
        _recreate_database(url)
    except OperationalError as exc:
        pytest.fail(f"Test database unreachable. Run `docker compose up -d db`.\n{exc}")

    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    eng = create_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Session:
    """Each test runs inside a transaction that is rolled back.

    The stores are append-only (UPDATE/DELETE/TRUNCATE are rejected by
    trigger), so rollback is the only way to clean up.
    """
    with engine.connect() as conn:
        outer = conn.begin()
        sess = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield sess
        finally:
            sess.close()
            outer.rollback()
