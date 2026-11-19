"""FastAPI application. Interactive docs (Swagger UI) at /docs."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import services
from app.cases import InvalidTransition, allowed_decisions
from app.config import get_settings
from app.db import get_session
from app.models import Case
from app.schemas import (
    AuditOut,
    CaseDetailOut,
    CaseSummaryOut,
    DecisionRequest,
    Page,
    ScreeningOut,
    ScreeningRequest,
    SummaryOut,
)
from app.summary import generate_summary

app = FastAPI(
    title="Sanctions Screener",
    version="0.1.0",
    description=(
        "Screen people and companies against the OFAC SDN and UN Security Council sanctions lists, "
        "and work the resulting alerts as cases. Authenticate with the `X-API-Key` header "
        "(click **Authorize**)."
    ),
)

SessionDep = Annotated[Session, Depends(get_session)]


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    # compare_digest: constant-time comparison, no timing side channel.
    if x_api_key is None or not secrets.compare_digest(x_api_key, get_settings().api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or invalid X-API-Key")


Auth = Depends(require_api_key)


@app.exception_handler(services.NotFound)
def _not_found(_, exc: services.NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(InvalidTransition)
def _conflict(_, exc: InvalidTransition):
    return JSONResponse(status_code=409, content={
        "detail": str(exc), "status": exc.status, "allowed_decisions": allowed_decisions(exc.status)})


@app.get("/health", tags=["ops"])
def health(session: SessionDep) -> dict:
    session.execute(text("SELECT 1"))
    # Latest ingestion per source list (DISTINCT ON is Postgres-specific).
    latest = session.execute(text(
        "SELECT DISTINCT ON (source) source, version, entity_count, ingested_at "
        "FROM list_versions ORDER BY source, ingested_at DESC, id DESC"
    )).all()
    return {"status": "ok", "database": "ok",
            "lists": [{"source": r.source, "version": r.version, "entities": r.entity_count,
                       "ingested_at": r.ingested_at.isoformat()} for r in latest]}


@app.post("/screenings", response_model=ScreeningOut, status_code=201, dependencies=[Auth], tags=["screening"])
def create_screening(
    req: ScreeningRequest,
    session: SessionDep,
    response: Response,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> ScreeningOut:
    """Screen one person/company. Hits at or above the threshold automatically open a case.

    Send an `Idempotency-Key` header to make retries safe: the same key returns the
    original result (`Idempotent-Replay: true`) instead of opening a duplicate case.
    """
    result, replayed = services.create_screening(session, req, idempotency_key)
    if replayed:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    return result


@app.get("/screenings/{screening_id}", response_model=ScreeningOut, dependencies=[Auth], tags=["screening"])
def get_screening(screening_id: int, session: SessionDep) -> ScreeningOut:
    return services.get_screening(session, screening_id)


@app.get("/cases", response_model=Page[CaseSummaryOut], dependencies=[Auth], tags=["cases"])
def list_cases(
    session: SessionDep,
    status_: Annotated[str | None, Query(alias="status", pattern="^(open|escalated|cleared|confirmed_match)$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[CaseSummaryOut]:
    items, total = services.list_cases(session, status_, limit, offset)
    return Page[CaseSummaryOut](items=items, total=total, limit=limit, offset=offset)


@app.get("/cases/stats", dependencies=[Auth], tags=["cases"])
def case_stats(session: SessionDep) -> dict[str, int]:
    rows = session.execute(select(Case.status, func.count()).group_by(Case.status)).all()
    return {s: c for s, c in rows}


@app.get("/cases/{case_id}", response_model=CaseDetailOut, dependencies=[Auth], tags=["cases"])
def get_case(case_id: int, session: SessionDep) -> CaseDetailOut:
    return services.get_case(session, case_id)


@app.post("/cases/{case_id}/decision", response_model=CaseDetailOut, dependencies=[Auth], tags=["cases"])
def decide(case_id: int, req: DecisionRequest, session: SessionDep) -> CaseDetailOut:
    """Apply `clear`, `escalate` or `confirm_match`. Invalid transitions return **409**."""
    return services.decide(session, case_id, req)


@app.get("/cases/{case_id}/audit", response_model=list[AuditOut], dependencies=[Auth], tags=["cases"])
def case_audit(case_id: int, session: SessionDep) -> list[AuditOut]:
    return services.audit_entries(session, case_id)


@app.post("/cases/{case_id}/summary", response_model=SummaryOut, dependencies=[Auth], tags=["cases"])
def summarise(case_id: int, session: SessionDep) -> SummaryOut:
    """Write an analyst note from the match evidence (Claude if ANTHROPIC_API_KEY is set, else a template)."""
    case = services.get_case(session, case_id)
    summary_text, source = generate_summary(case)
    services.save_summary(session, case_id, summary_text, source)
    return SummaryOut(case_id=case_id, summary=summary_text, source=source)
