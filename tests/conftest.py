"""Shared fixtures. API/DB tests run against a REAL PostgreSQL database (pg_trgm
can't be faked with SQLite): locally the portable server from scripts/localpg.py,
in CI a postgres:16 service container.

Point TEST_DATABASE_URL elsewhere if needed. The database is wiped and migrated
from scratch once per test session, then loaded with tests/fixtures/.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:54329/screener_test")
# Must be set before any app module reads settings (get_settings is cached).
os.environ["DATABASE_URL"] = TEST_DB
os.environ["API_KEY"] = "test-key"
os.environ["ANTHROPIC_API_KEY"] = ""  # summary tests must exercise the template fallback only
os.environ["MATCH_THRESHOLD"] = "0.88"

FIXTURES = Path(__file__).parent / "fixtures"
HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture(scope="session")
def engine():
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from app.db import get_engine

    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent / "alembic"))
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    return eng


@pytest.fixture(scope="session")
def loaded(engine):
    """Fixture lists loaded once per session (300 OFAC + 30 UN entities)."""
    from app.db import get_sessionmaker
    from app.ingest.loader import load_records
    from app.ingest.parsers import parse_ofac, parse_un

    with get_sessionmaker()() as session:
        ofac, v1 = parse_ofac(FIXTURES / "ofac")
        un, v2 = parse_un(FIXTURES / "un_sample.xml")
        return {"ofac": load_records(session, ofac, "OFAC_SDN", v1), "un": load_records(session, un, "UN_SC", v2)}


@pytest.fixture()
def db(loaded):
    """A session on a database with lists loaded and no screenings/cases/audit rows."""
    from sqlalchemy import text

    from app.db import get_engine, get_sessionmaker

    with get_engine().begin() as conn:
        # TRUNCATE does not fire the row-level append-only trigger.
        conn.execute(text("TRUNCATE audit_log, cases, screening_hits, screenings RESTART IDENTITY CASCADE"))
    session = get_sessionmaker()()
    yield session
    session.close()


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
