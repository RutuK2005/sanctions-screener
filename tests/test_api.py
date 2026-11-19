"""End-to-end API tests: FastAPI TestClient -> SQLAlchemy -> real PostgreSQL."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import HEADERS

ZAWAHIRI = {"name": "Ayman al-Zawahiri", "entity_type": "individual"}


def screen(client, body, **headers):
    return client.post("/screenings", json=body, headers={**HEADERS, **headers})


def open_case(client, name="Ayman al-Zawahiri") -> int:
    r = screen(client, {"name": name})
    assert r.status_code == 201, r.text
    assert r.json()["case_id"] is not None
    return r.json()["case_id"]


def decide(client, case_id, decision, note="checked identifiers"):
    return client.post(f"/cases/{case_id}/decision", headers=HEADERS,
                       json={"decision": decision, "reviewer": "analyst.test", "note": note})


# ------------------------------------------------------------------ ops/auth

def test_health_is_public_and_reports_lists(client):
    r = client.get("/health")
    assert r.status_code == 200
    sources = {row["source"]: row["entities"] for row in r.json()["lists"]}
    assert sources == {"OFAC_SDN": 300, "UN_SC": 30}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_auth_required(client, headers):
    assert client.post("/screenings", json=ZAWAHIRI, headers=headers).status_code == 401
    assert client.get("/cases", headers=headers).status_code == 401


# ---------------------------------------------------------------- screening

def test_screening_hit_opens_case_with_explanation(client):
    r = screen(client, ZAWAHIRI)
    assert r.status_code == 201
    body = r.json()
    assert body["hit_count"] >= 1 and body["case_id"] is not None
    top = body["hits"][0]
    assert top["source"] == "OFAC_SDN" and top["source_id"] == "2676"
    assert top["score"] >= body["threshold"]
    exp = top["explanation"]
    assert exp["query_normalized"] == "ayman al zawahiri"
    assert {"dob", "country", "type"} == set(exp["adjustments"])


def test_fuzzy_variant_still_hits(client):
    body = screen(client, {"name": "Zawahri, Aiman"}).json()
    assert any(h["source_id"] == "2676" for h in body["hits"])


def test_clean_name_opens_no_case(client):
    body = screen(client, {"name": "Priya Raghavan Iyer", "entity_type": "individual"}).json()
    assert body["hit_count"] == 0 and body["case_id"] is None and body["hits"] == []
    assert client.get("/cases", headers=HEADERS).json()["total"] == 0


def test_dob_agreement_beats_dob_conflict(client):
    good = screen(client, {**ZAWAHIRI, "dob": "1951-06-19", "threshold": 0.5}).json()["hits"][0]
    bad = screen(client, {**ZAWAHIRI, "dob": "1990-01-01", "threshold": 0.5}).json()["hits"][0]
    assert good["score"] > bad["score"]
    assert bad["explanation"]["adjustments"]["dob"]["value"] < 0


@pytest.mark.parametrize("body", [{"name": ""}, {"name": "x", "dob": "19-06-1951"},
                                  {"name": "x", "entity_type": "robot"}, {"name": "x", "threshold": 2}])
def test_validation_errors(client, body):
    assert screen(client, body).status_code == 422


def test_idempotency_key_replays_instead_of_duplicating(client):
    first = screen(client, ZAWAHIRI, **{"Idempotency-Key": "req-123"})
    second = screen(client, ZAWAHIRI, **{"Idempotency-Key": "req-123"})
    assert first.status_code == 201 and second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert first.json()["id"] == second.json()["id"]
    assert client.get("/cases", headers=HEADERS).json()["total"] == 1


def test_get_screening(client):
    sid = screen(client, ZAWAHIRI).json()["id"]
    assert client.get(f"/screenings/{sid}", headers=HEADERS).json()["id"] == sid
    assert client.get("/screenings/999999", headers=HEADERS).status_code == 404


# -------------------------------------------------------------------- cases

def test_list_cases_filter_and_pagination(client):
    ids = [open_case(client) for _ in range(3)]
    assert decide(client, ids[0], "clear").status_code == 200

    page = client.get("/cases?limit=2&offset=0", headers=HEADERS).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert page["items"][0]["id"] == ids[-1]  # newest first
    page2 = client.get("/cases?limit=2&offset=2", headers=HEADERS).json()
    assert len(page2["items"]) == 1

    open_cases = client.get("/cases?status=open", headers=HEADERS).json()
    assert open_cases["total"] == 2
    cleared = client.get("/cases?status=cleared", headers=HEADERS).json()
    assert [c["id"] for c in cleared["items"]] == [ids[0]]
    assert client.get("/cases?status=bogus", headers=HEADERS).status_code == 422
    assert client.get("/cases/stats", headers=HEADERS).json() == {"open": 2, "cleared": 1}


def test_case_detail(client):
    cid = open_case(client)
    detail = client.get(f"/cases/{cid}", headers=HEADERS).json()
    assert detail["status"] == "open"
    assert detail["allowed_decisions"] == ["clear", "confirm_match", "escalate"]
    assert detail["screening"]["hits"][0]["source_id"] == "2676"
    assert [a["action"] for a in detail["audit"]] == ["case_opened"]
    assert client.get("/cases/999999", headers=HEADERS).status_code == 404


def test_escalate_then_confirm_then_reject_further_changes(client):
    cid = open_case(client)
    r = decide(client, cid, "escalate", "DOB matches, need senior review")
    assert r.status_code == 200 and r.json()["status"] == "escalated"
    assert r.json()["allowed_decisions"] == ["clear", "confirm_match"]

    assert decide(client, cid, "escalate").status_code == 409  # already escalated
    r = decide(client, cid, "confirm_match", "passport number matches listing")
    assert r.status_code == 200 and r.json()["status"] == "confirmed_match"

    r = decide(client, cid, "clear")
    assert r.status_code == 409
    assert r.json()["status"] == "confirmed_match" and r.json()["allowed_decisions"] == []

    trail = client.get(f"/cases/{cid}/audit", headers=HEADERS).json()
    assert [(a["action"], a["from_status"], a["to_status"]) for a in trail] == [
        ("case_opened", None, "open"),
        ("decision:escalate", "open", "escalated"),
        ("decision:confirm_match", "escalated", "confirmed_match"),
    ]
    assert trail[1]["actor"] == "analyst.test" and trail[1]["note"] == "DOB matches, need senior review"


def test_decision_validation(client):
    cid = open_case(client)
    bad = client.post(f"/cases/{cid}/decision", headers=HEADERS,
                      json={"decision": "delete", "reviewer": "a", "note": "nope"})
    assert bad.status_code == 422
    missing_note = client.post(f"/cases/{cid}/decision", headers=HEADERS,
                               json={"decision": "clear", "reviewer": "a", "note": ""})
    assert missing_note.status_code == 422
    assert decide(client, 999999, "clear").status_code == 404


def test_audit_log_is_append_only_in_the_database(client, db):
    open_case(client)
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE audit_log SET note = 'tampered'"))
    db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("DELETE FROM audit_log"))
    db.rollback()


def test_screenings_are_audited_even_without_a_case(client, db):
    screen(client, {"name": "Priya Raghavan Iyer"})
    actions = db.execute(text("SELECT action, case_id FROM audit_log")).all()
    assert actions == [("screening_created", None)]


# ------------------------------------------------------------------ summary

def test_summary_uses_template_without_api_key(client):
    cid = open_case(client)
    r = client.post(f"/cases/{cid}/summary", headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "template"
    assert "AL ZAWAHIRI" in body["summary"] and "OFAC_SDN 2676" in body["summary"]
    detail = client.get(f"/cases/{cid}", headers=HEADERS).json()
    assert detail["summary"] == body["summary"] and detail["summary_source"] == "template"
    assert detail["audit"][-1]["action"] == "summary_generated"
