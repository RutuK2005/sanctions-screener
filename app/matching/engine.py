"""Screening engine: Postgres retrieves candidates cheaply, Python scores them carefully.

Stage 1 - retrieval (SQL, GIN trigram index, ~ms):
    name_norm %  :q   -> trigram similarity(name_norm, q)       >= pg_trgm.similarity_threshold
    :q <% name_norm   -> word_similarity(q, name_norm)          >= pg_trgm.word_similarity_threshold
  The second operator catches short queries against long list names
  ("ayman zawahiri" vs "ayman muhammad rabi al zawahiri"). Both are index-assisted.

Stage 2 - re-scoring (Python, rapidfuzz) of at most `candidate_limit` names,
  then collapse to the best-scoring alias per listed entity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.matching.normalize import normalize
from app.matching.scoring import score_candidate

# Written as a UNION of two separately index-assisted lookups on purpose: with
# `WHERE a % q OR q <% a` the planner sometimes picked a sequential scan over
# every name (~360 ms); the UNION form keeps both GIN bitmap scans (~25 ms).
CANDIDATE_SQL = text(
    """
    WITH candidate_ids AS (
        SELECT id FROM entity_names WHERE name_norm % :q
        UNION
        SELECT id FROM entity_names WHERE :q <% name_norm
    )
    SELECT n.entity_id, n.name, n.name_norm, n.name_type,
           greatest(similarity(n.name_norm, :q), word_similarity(:q, n.name_norm)) AS trgm,
           e.entity_type, e.dobs, e.countries, e.primary_name, e.source, e.source_id, e.programs
    FROM candidate_ids c
    JOIN entity_names n ON n.id = c.id
    JOIN sanctioned_entities e ON e.id = n.entity_id
    WHERE e.active
    ORDER BY trgm DESC
    LIMIT :limit
    """
)


@dataclass
class Hit:
    entity_id: int
    source: str
    source_id: str
    primary_name: str
    entity_type: str
    programs: list[str]
    matched_name: str
    matched_name_type: str
    score: float
    trigram: float = 0.0  # best raw pg_trgm similarity over this entity's names (baseline signal)
    explanation: dict = field(default_factory=dict)


@dataclass
class ScreenResult:
    query_norm: str
    candidates_considered: int
    hits: list[Hit]  # best alias per entity, highest score first (not yet thresholded)
    latency_ms: float


def screen(
    session: Session,
    name: str,
    *,
    entity_type: str | None = None,
    dob: str | None = None,
    country: str | None = None,
    max_results: int = 10,
) -> ScreenResult:
    settings = get_settings()
    started = time.perf_counter()
    q = normalize(name)
    if not q:
        return ScreenResult(q, 0, [], 0.0)

    # SET LOCAL equivalents: thresholds apply to this transaction only.
    session.execute(text("SELECT set_config('pg_trgm.similarity_threshold', :t, true)"),
                    {"t": str(settings.trgm_threshold)})
    session.execute(text("SELECT set_config('pg_trgm.word_similarity_threshold', :t, true)"),
                    {"t": str(max(settings.trgm_threshold, 0.5))})
    rows = session.execute(CANDIDATE_SQL, {"q": q, "limit": settings.candidate_limit}).mappings().all()

    best: dict[int, Hit] = {}
    for row in rows:
        score, explanation = score_candidate(
            query_norm=q,
            cand_norm=row["name_norm"],
            query_dob=dob,
            query_country=country,
            query_type=entity_type,
            entity_type=row["entity_type"],
            list_dobs=row["dobs"] or [],
            list_countries=row["countries"] or [],
            trigram_similarity=row["trgm"],
        )
        current = best.get(row["entity_id"])
        trgm = max(row["trgm"], current.trigram if current else 0.0)
        if current is not None:
            current.trigram = trgm
        if current is None or score > current.score:
            best[row["entity_id"]] = Hit(
                entity_id=row["entity_id"],
                source=row["source"],
                source_id=row["source_id"],
                primary_name=row["primary_name"],
                entity_type=row["entity_type"],
                programs=row["programs"] or [],
                matched_name=row["name"],
                matched_name_type=row["name_type"],
                score=round(score, 4),
                trigram=trgm,
                explanation=explanation,
            )
    hits = sorted(best.values(), key=lambda h: h.score, reverse=True)[:max_results]
    return ScreenResult(q, len(rows), hits, (time.perf_counter() - started) * 1000)
