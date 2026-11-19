"""Business logic used by the API routes (kept out of the route functions so it
can be tested and reused by the CLI)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.cases import allowed_decisions, next_status
from app.config import get_settings
from app.matching.engine import screen
from app.models import AuditLog, Case, Screening, ScreeningHit
from app.schemas import (
    AuditOut,
    CaseDetailOut,
    CaseSummaryOut,
    DecisionRequest,
    HitOut,
    ScreeningOut,
    ScreeningRequest,
)


class NotFound(Exception):
    pass


# ---------------------------------------------------------------- screenings

def _load_screening(session: Session, screening_id: int) -> Screening:
    stmt = (select(Screening).where(Screening.id == screening_id)
            .options(selectinload(Screening.hits).selectinload(ScreeningHit.entity), selectinload(Screening.case)))
    screening = session.scalars(stmt).one_or_none()
    if screening is None:
        raise NotFound(f"screening {screening_id} not found")
    return screening


def screening_out(s: Screening) -> ScreeningOut:
    return ScreeningOut(
        id=s.id, query_name=s.query_name, query_type=s.query_type, query_dob=s.query_dob,
        query_country=s.query_country, threshold=s.threshold, top_score=s.top_score, hit_count=s.hit_count,
        hits=[HitOut(entity_id=h.entity_id, source=h.entity.source, source_id=h.entity.source_id,
                     primary_name=h.entity.primary_name, entity_type=h.entity.entity_type,
                     programs=h.entity.programs, matched_name=h.matched_name, score=h.score,
                     explanation=h.explanation) for h in s.hits],
        case_id=s.case.id if s.case else None, latency_ms=s.latency_ms, created_at=s.created_at,
    )


def create_screening(session: Session, req: ScreeningRequest, idempotency_key: str | None,
                     actor: str = "api") -> tuple[ScreeningOut, bool]:
    """Screen a name, persist the result, open a case if anything is above threshold.

    Returns (screening, replayed). With an Idempotency-Key, a retried request
    returns the stored result instead of creating a second screening/case.
    """
    if idempotency_key:
        existing = session.scalar(select(Screening.id).where(Screening.idempotency_key == idempotency_key))
        if existing is not None:
            return screening_out(_load_screening(session, existing)), True

    threshold = req.threshold if req.threshold is not None else get_settings().match_threshold
    result = screen(session, req.name, entity_type=req.entity_type, dob=req.dob, country=req.country)
    hits = [h for h in result.hits if h.score >= threshold]

    screening = Screening(
        idempotency_key=idempotency_key, query_name=req.name, query_type=req.entity_type, query_dob=req.dob,
        query_country=req.country, threshold=threshold,
        top_score=result.hits[0].score if result.hits else 0.0, hit_count=len(hits),
        latency_ms=round(result.latency_ms, 2),
    )
    session.add(screening)
    session.flush()
    for h in hits:
        session.add(ScreeningHit(screening_id=screening.id, entity_id=h.entity_id, score=h.score,
                                 matched_name=h.matched_name, explanation=h.explanation))
    session.add(AuditLog(action="screening_created", actor=actor,
                         details={"screening_id": screening.id, "hit_count": len(hits),
                                  "top_score": screening.top_score, "threshold": threshold}))
    if hits:
        case = Case(screening_id=screening.id, status="open", top_score=hits[0].score)
        session.add(case)
        session.flush()
        session.add(AuditLog(case_id=case.id, action="case_opened", actor="system", to_status="open",
                             details={"screening_id": screening.id, "hit_count": len(hits)}))
    try:
        session.commit()
    except IntegrityError:
        # Two concurrent requests with the same Idempotency-Key: the unique
        # constraint lets exactly one win; the loser returns the winner's result.
        session.rollback()
        if not idempotency_key:
            raise
        existing = session.scalar(select(Screening.id).where(Screening.idempotency_key == idempotency_key))
        return screening_out(_load_screening(session, existing)), True
    return screening_out(_load_screening(session, screening.id)), False


def get_screening(session: Session, screening_id: int) -> ScreeningOut:
    return screening_out(_load_screening(session, screening_id))


# --------------------------------------------------------------------- cases

def _case_summary(case: Case) -> CaseSummaryOut:
    return CaseSummaryOut(id=case.id, status=case.status, top_score=case.top_score,
                          query_name=case.screening.query_name, hit_count=case.screening.hit_count,
                          created_at=case.created_at, updated_at=case.updated_at)


def list_cases(session: Session, status: str | None, limit: int, offset: int) -> tuple[list[CaseSummaryOut], int]:
    base = select(Case)
    if status:
        base = base.where(Case.status == status)
    total = session.scalar(select(func.count()).select_from(base.subquery()))
    rows = session.scalars(base.options(selectinload(Case.screening))
                           .order_by(Case.created_at.desc(), Case.id.desc()).limit(limit).offset(offset)).all()
    return [_case_summary(c) for c in rows], total


def _load_case(session: Session, case_id: int, *, for_update: bool = False) -> Case:
    stmt = select(Case).where(Case.id == case_id)
    if for_update:
        # Row lock: two reviewers deciding at once are serialised, and the
        # second one sees the first one's status (and gets a 409).
        stmt = stmt.with_for_update()
    case = session.scalars(stmt).one_or_none()
    if case is None:
        raise NotFound(f"case {case_id} not found")
    return case


def get_case(session: Session, case_id: int) -> CaseDetailOut:
    case = _load_case(session, case_id)
    screening = _load_screening(session, case.screening_id)
    audit = session.scalars(select(AuditLog).where(AuditLog.case_id == case_id).order_by(AuditLog.id)).all()
    return CaseDetailOut(
        **_case_summary(case).model_dump(),
        screening=screening_out(screening),
        allowed_decisions=allowed_decisions(case.status),
        summary=case.summary, summary_source=case.summary_source,
        audit=[AuditOut.model_validate(a) for a in audit],
    )


def decide(session: Session, case_id: int, req: DecisionRequest) -> CaseDetailOut:
    case = _load_case(session, case_id, for_update=True)
    new_status = next_status(case.status, req.decision)  # raises InvalidTransition -> 409
    session.add(AuditLog(case_id=case.id, action=f"decision:{req.decision}", actor=req.reviewer,
                         from_status=case.status, to_status=new_status, note=req.note))
    case.status = new_status
    case.updated_at = func.now()
    session.commit()
    return get_case(session, case_id)


def save_summary(session: Session, case_id: int, text: str, source: str, actor: str = "api") -> None:
    case = _load_case(session, case_id, for_update=True)
    case.summary, case.summary_source = text, source
    session.add(AuditLog(case_id=case_id, action="summary_generated", actor=actor, details={"source": source}))
    session.commit()


def audit_entries(session: Session, case_id: int) -> list[AuditOut]:
    _load_case(session, case_id)
    rows = session.scalars(select(AuditLog).where(AuditLog.case_id == case_id).order_by(AuditLog.id)).all()
    return [AuditOut.model_validate(a) for a in rows]
