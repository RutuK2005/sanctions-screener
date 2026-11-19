from pathlib import Path

import pytest
from defusedxml import EntitiesForbidden

from app.ingest.parsers import countries_from_remarks, dobs_from_remarks, parse_ofac, parse_ofac_date, parse_un

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("19 Jun 1951", ["1951-06-19"]), ("Aug 1946", ["1946-08"]), ("1955", ["1955"]),
     ("circa 1960", ["1960"]), ("1955 to 1957", ["1955", "1956", "1957"]), ("unknown", [])],
)
def test_parse_ofac_date(text, expected):
    assert parse_ofac_date(text) == expected


def test_remarks_extraction():
    remarks = "DOB 21 Apr 1983; alt. DOB 1984; POB Zefta, Lebanon; nationality Lebanon; citizen Syria."
    assert dobs_from_remarks(remarks) == ["1983-04-21", "1984"]
    assert countries_from_remarks(remarks) == ["Lebanon", "Syria"]


def test_parse_ofac_fixture():
    records, version = parse_ofac(FIXTURES / "ofac")
    assert len(records) == 300
    assert version.startswith("sha256:")
    by_id = {r.source_id: r for r in records}
    zawahiri = by_id["2676"]
    assert zawahiri.entity_type == "individual"
    assert zawahiri.dobs == ["1951-06-19"]
    assert zawahiri.programs == ["SDGT"]
    assert ("AL-ZAWAHIRI, Ayman", "aka") in zawahiri.aliases
    bank = by_id["4632"]
    assert bank.entity_type == "entity"
    assert {"IRAN", "SDGT", "IRGC"} <= set(bank.programs)


def test_parse_un_fixture():
    records, version = parse_un(FIXTURES / "un_sample.xml")
    assert len(records) == 30
    assert version[:4].isdigit()  # dateGenerated attribute
    assert {r.entity_type for r in records} == {"individual", "entity"}
    assert all(r.source == "UN_SC" and r.source_id for r in records)


def test_un_parser_rejects_entity_expansion(tmp_path):
    bomb = tmp_path / "bomb.xml"
    bomb.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><CONSOLIDATED_LIST>&a;</CONSOLIDATED_LIST>')
    with pytest.raises(EntitiesForbidden):
        parse_un(bomb)
