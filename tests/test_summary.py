"""Summary generation: only the deterministic fallback paths are tested (no network)."""

from datetime import UTC, datetime

from app import summary
from app.config import get_settings
from app.schemas import CaseDetailOut, HitOut, ScreeningOut

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def make_case(hits):
    screening = ScreeningOut(id=1, query_name="Aiman Zawahri", query_type="individual", query_dob="1951-06-19",
                             query_country=None, threshold=0.88, top_score=hits[0].score if hits else 0.0,
                             hit_count=len(hits), hits=hits, case_id=1, latency_ms=5.0, created_at=NOW)
    return CaseDetailOut(id=1, status="open", top_score=screening.top_score, query_name="Aiman Zawahri",
                         hit_count=len(hits), created_at=NOW, updated_at=NOW, screening=screening,
                         allowed_decisions=["clear"], summary=None, summary_source=None, audit=[])


HIT = HitOut(entity_id=1, source="OFAC_SDN", source_id="2676", primary_name="AL ZAWAHIRI, Dr. Ayman",
             entity_type="individual", programs=["SDGT"], matched_name="AL-ZAWAHIRI, Ayman", score=0.95,
             explanation={"name": {"score": 0.9}, "adjustments": {
                 "dob": {"value": 0.05, "reason": "exact DOB match 1951-06-19"},
                 "country": {"value": 0.0, "reason": "not compared (missing on one side)"},
                 "type": {"value": 0.0, "reason": "type agrees"}}})


def test_template_is_deterministic_and_cites_evidence():
    case = make_case([HIT])
    text = summary.template_summary(case)
    assert text == summary.template_summary(case)
    assert "OFAC_SDN 2676" in text and "SDGT" in text and "exact DOB match" in text and "+0.05" in text


def test_template_with_no_hits():
    assert "no hits" in summary.template_summary(make_case([]))


def test_generate_without_key_uses_template(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")
    text, source = summary.generate_summary(make_case([HIT]))
    assert source == "template" and "AL ZAWAHIRI" in text


def test_generate_falls_back_when_llm_fails(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "sk-test-not-real")
    monkeypatch.setattr(summary, "llm_summary", lambda *a, **k: None)  # simulate API error/refusal
    text, source = summary.generate_summary(make_case([HIT]))
    assert source == "template"


def test_evidence_payload_contains_only_case_data():
    ev = summary.evidence(make_case([HIT]))
    assert ev["query"]["name"] == "Aiman Zawahri"
    assert ev["hits"][0]["source_id"] == "2676"
