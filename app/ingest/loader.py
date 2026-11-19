"""Idempotent load of parsed records into Postgres.

Re-running the same file is a no-op in effect: entities are upserted on
(source, source_id), their names are replaced, and anything from the same
source that is *not* in this file is marked inactive (delisted) rather than
deleted - old screenings and cases keep pointing at a real row.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, func, insert, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.ingest.parsers import EntityRecord
from app.matching.normalize import normalize
from app.models import EntityName, ListVersion, SanctionedEntity

BATCH = 1000


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def load_records(session: Session, records: list[EntityRecord], source: str, version: str) -> dict:
    # One row per (source, source_id): ON CONFLICT can't touch the same row twice in a statement.
    records = list({r.source_id: r for r in records}.values())
    name_count = 0
    seen_ids: list[int] = []
    for chunk in _chunks(records, BATCH):
        rows = [
            {
                "source": r.source,
                "source_id": r.source_id,
                "entity_type": r.entity_type,
                "primary_name": r.primary_name,
                "programs": r.programs,
                "countries": r.countries,
                "dobs": r.dobs,
                "remarks": r.remarks,
                "list_version": version,
                "active": True,
            }
            for r in chunk
        ]
        stmt = pg_insert(SanctionedEntity).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_entity_source_id",
            set_={c: stmt.excluded[c] for c in
                  ("entity_type", "primary_name", "programs", "countries", "dobs", "remarks", "list_version", "active")}
            | {"updated_at": func.now()},
        ).returning(SanctionedEntity.id, SanctionedEntity.source_id)
        id_by_source_id = {sid: eid for eid, sid in session.execute(stmt)}
        ids = list(id_by_source_id.values())
        seen_ids.extend(ids)

        session.execute(delete(EntityName).where(EntityName.entity_id.in_(ids)))
        name_rows = []
        for r in chunk:
            eid = id_by_source_id[r.source_id]
            seen_norms: set[str] = set()
            for name, name_type in [(r.primary_name, "primary"), *r.aliases]:
                norm = normalize(name)
                if not norm or norm in seen_norms:
                    continue
                seen_norms.add(norm)
                name_rows.append({"entity_id": eid, "name": name, "name_norm": norm, "name_type": name_type})
        for name_chunk in _chunks(name_rows, 5000):
            session.execute(insert(EntityName), name_chunk)
        name_count += len(name_rows)

    delisted = session.execute(
        update(SanctionedEntity)
        .where(SanctionedEntity.source == source, SanctionedEntity.list_version != version,
               SanctionedEntity.active.is_(True))
        .values(active=False, updated_at=func.now())
    ).rowcount
    session.add(ListVersion(source=source, version=version, entity_count=len(seen_ids), name_count=name_count))
    session.commit()
    # Refresh planner statistics after a bulk rewrite so trigram queries keep
    # choosing the GIN index.
    session.execute(text("ANALYZE entity_names"))
    session.execute(text("ANALYZE sanctioned_entities"))
    session.commit()
    return {"source": source, "version": version, "entities": len(seen_ids), "names": name_count,
            "delisted": delisted}
