"""Ingestion against real Postgres: idempotent upsert + delisting."""

from sqlalchemy import func, select

from app.ingest.loader import load_records
from app.ingest.parsers import parse_ofac
from app.models import EntityName, ListVersion, SanctionedEntity
from tests.conftest import FIXTURES


def _counts(session):
    entities = session.scalar(select(func.count()).select_from(SanctionedEntity)
                              .where(SanctionedEntity.source == "OFAC_SDN", SanctionedEntity.active))
    names = session.scalar(select(func.count()).select_from(EntityName))
    return entities, names


def test_reload_is_idempotent(db):
    before = _counts(db)
    records, version = parse_ofac(FIXTURES / "ofac")
    stats = load_records(db, records, "OFAC_SDN", version)
    assert stats["entities"] == 300 and stats["delisted"] == 0
    assert _counts(db) == before


def test_entities_missing_from_new_version_are_delisted_not_deleted(db):
    records, _ = parse_ofac(FIXTURES / "ofac")
    try:
        stats = load_records(db, records[:-5], "OFAC_SDN", "test-version-without-last-5")
        assert stats["delisted"] == 5
        inactive = db.scalars(select(SanctionedEntity.source_id).where(SanctionedEntity.active.is_(False))).all()
        assert sorted(inactive) == sorted(r.source_id for r in records[-5:])
        assert db.scalar(select(func.count()).select_from(ListVersion)
                         .where(ListVersion.version == "test-version-without-last-5")) == 1
    finally:  # restore the full fixture for other tests
        records, version = parse_ofac(FIXTURES / "ofac")
        load_records(db, records, "OFAC_SDN", version)
    assert _counts(db)[0] == 300


def test_names_are_normalised_and_indexed(db):
    norms = db.scalars(select(EntityName.name_norm).join(SanctionedEntity)
                       .where(SanctionedEntity.source_id == "2676")).all()
    assert "ayman al zawahiri" in norms
