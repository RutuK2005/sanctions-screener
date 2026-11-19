"""Parsers for the public sanctions lists -> a common EntityRecord shape.

* OFAC SDN "legacy" CSV trio (no header row):
    sdn.csv  ent_num, SDN_Name, SDN_Type, Program, Title, Call_Sign, Vess_type,
             Tonnage, GRT, Vess_flag, Vess_owner, Remarks
    alt.csv  ent_num, alt_num, alt_type, alt_name, alt_remarks
    add.csv  ent_num, add_num, address, city/state/zip, country, add_remarks
  "-0-" means NULL. DOBs and nationalities live inside free-text Remarks.
* UN Security Council consolidated list XML.

Downloaded files are untrusted input: CSV is parsed with the csv module only,
XML with defusedxml (blocks entity-expansion / external-entity attacks).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from defusedxml import ElementTree as DET

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


@dataclass
class EntityRecord:
    source: str
    source_id: str
    entity_type: str  # individual | entity | vessel | aircraft
    primary_name: str
    aliases: list[tuple[str, str]] = field(default_factory=list)  # (name, alias_type)
    programs: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    dobs: list[str] = field(default_factory=list)
    remarks: str | None = None


def _null(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return None if value in ("", "-0-") else value


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for item in items:
        if item and item.casefold() not in {k.casefold() for k in seen}:
            seen[item] = None
    return list(seen)


# ------------------------------------------------------------------ dates

_DOB_RE = re.compile(r"\bDOB ([^;]+)")
_DAY_MON_YEAR = re.compile(r"^(\d{1,2}) ([A-Za-z]{3}) (\d{4})$")
_MON_YEAR = re.compile(r"^([A-Za-z]{3}) (\d{4})$")
_YEAR = re.compile(r"^(?:circa )?(\d{4})$")
_RANGE = re.compile(r"^(?:circa )?(\d{4}) to (\d{4})$")


def parse_ofac_date(text: str) -> list[str]:
    """'19 Jun 1951' -> ['1951-06-19'], 'Aug 1946' -> ['1946-08'], '1955 to 1957' -> 3 years."""
    text = text.strip().rstrip(".")
    if m := _DAY_MON_YEAR.match(text):
        mon = MONTHS.get(m[2].lower())
        return [f"{m[3]}-{mon:02d}-{int(m[1]):02d}"] if mon else []
    if m := _MON_YEAR.match(text):
        mon = MONTHS.get(m[1].lower())
        return [f"{m[2]}-{mon:02d}"] if mon else []
    if m := _YEAR.match(text):
        return [m[1]]
    if m := _RANGE.match(text):
        start, end = int(m[1]), int(m[2])
        if 0 <= end - start <= 10:
            return [str(y) for y in range(start, end + 1)]
    return []


def dobs_from_remarks(remarks: str | None) -> list[str]:
    if not remarks:
        return []
    out: list[str] = []
    for m in _DOB_RE.finditer(remarks):
        out.extend(parse_ofac_date(m[1]))
    return _dedupe(out)


_NATIONALITY_RE = re.compile(r"\b(?:nationality|citizen) ([A-Z][A-Za-z ,'()-]+?)(?:;|$|\.\s|\.$)")


def countries_from_remarks(remarks: str | None) -> list[str]:
    if not remarks:
        return []
    return _dedupe([m[1].strip() for m in _NATIONALITY_RE.finditer(remarks)])


# ------------------------------------------------------------------- OFAC

OFAC_TYPES = {"individual": "individual", "vessel": "vessel", "aircraft": "aircraft"}


def _read_csv(path: Path) -> list[list[str]]:
    # OFAC files are cp1252-ish and end with a 0x1A EOF marker line.
    text = path.read_bytes().decode("utf-8", errors="replace")
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and row[0].strip().isdigit():
            rows.append(row)
    return rows


def files_version(*paths: Path) -> str:
    digest = hashlib.sha256()
    for p in paths:
        if p.exists():
            digest.update(p.read_bytes())
    return "sha256:" + digest.hexdigest()[:16]


def parse_ofac(directory: Path) -> tuple[list[EntityRecord], str]:
    sdn_path, alt_path, add_path = (directory / n for n in ("sdn.csv", "alt.csv", "add.csv"))
    aliases: dict[str, list[tuple[str, str]]] = defaultdict(list)
    if alt_path.exists():
        for row in _read_csv(alt_path):
            name = _null(row[3]) if len(row) > 3 else None
            if name:
                aliases[row[0].strip()].append((name, (_null(row[2]) or "aka").lower().replace(".", "")))
    addr_countries: dict[str, list[str]] = defaultdict(list)
    if add_path.exists():
        for row in _read_csv(add_path):
            country = _null(row[4]) if len(row) > 4 else None
            if country:
                addr_countries[row[0].strip()].append(country)

    records = []
    for row in _read_csv(sdn_path):
        if len(row) < 12:
            continue
        ent_num = row[0].strip()
        name = _null(row[1])
        if not name:
            continue
        remarks = _null(row[11])
        programs = [p.strip(" []") for p in (_null(row[3]) or "").split("] [") if p.strip(" []")]
        countries = countries_from_remarks(remarks) + addr_countries.get(ent_num, [])
        if flag := _null(row[9]):
            countries.append(flag)
        records.append(EntityRecord(
            source="OFAC_SDN",
            source_id=ent_num,
            entity_type=OFAC_TYPES.get((_null(row[2]) or "").lower(), "entity"),
            primary_name=name,
            aliases=aliases.get(ent_num, []),
            programs=programs,
            countries=_dedupe(countries),
            dobs=dobs_from_remarks(remarks),
            remarks=remarks,
        ))
    return records, files_version(sdn_path, alt_path, add_path)


# --------------------------------------------------------------------- UN

def _text(el, tag: str) -> str | None:
    child = el.find(tag)
    return _null(child.text) if child is not None and child.text else None


def parse_un(path: Path) -> tuple[list[EntityRecord], str]:
    root = DET.parse(str(path)).getroot()
    version = root.get("dateGenerated") or files_version(path)
    records: list[EntityRecord] = []

    for ind in root.iter("INDIVIDUAL"):
        parts = [_text(ind, t) for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
        name = " ".join(p for p in parts if p)
        if not name:
            continue
        aliases = []
        for al in ind.iter("INDIVIDUAL_ALIAS"):
            alias = _text(al, "ALIAS_NAME")
            if alias:
                quality = (_text(al, "QUALITY") or "").lower()
                aliases.append((alias, "weak_aka" if quality == "low" else "aka"))
        if original := _text(ind, "NAME_ORIGINAL_SCRIPT"):
            aliases.append((original, "original_script"))
        countries: list[str] = []
        for nat in ind.iter("NATIONALITY"):
            countries += [v.text.strip() for v in nat.iter("VALUE") if v.text and v.text.strip()]
        for addr in ind.iter("INDIVIDUAL_ADDRESS"):
            if c := _text(addr, "COUNTRY"):
                countries.append(c)
        dobs: list[str] = []
        for dob in ind.iter("INDIVIDUAL_DATE_OF_BIRTH"):
            if d := _text(dob, "DATE"):
                dobs.append(d[:10])
            elif y := _text(dob, "YEAR"):
                dobs.append(y)
            elif ((f := _text(dob, "FROM_YEAR")) and (t := _text(dob, "TO_YEAR"))
                  and f.isdigit() and t.isdigit() and 0 <= int(t) - int(f) <= 10):
                dobs += [str(y) for y in range(int(f), int(t) + 1)]
        records.append(EntityRecord(
            source="UN_SC",
            source_id=_text(ind, "REFERENCE_NUMBER") or _text(ind, "DATAID") or name,
            entity_type="individual",
            primary_name=name,
            aliases=aliases,
            programs=[p for p in [_text(ind, "UN_LIST_TYPE")] if p],
            countries=_dedupe(countries),
            dobs=_dedupe(dobs),
            remarks=_text(ind, "COMMENTS1"),
        ))

    for ent in root.iter("ENTITY"):
        name = _text(ent, "FIRST_NAME")
        if not name:
            continue
        aliases = [(a, "aka") for al in ent.iter("ENTITY_ALIAS") if (a := _text(al, "ALIAS_NAME"))]
        countries = [c for addr in ent.iter("ENTITY_ADDRESS") if (c := _text(addr, "COUNTRY"))]
        records.append(EntityRecord(
            source="UN_SC",
            source_id=_text(ent, "REFERENCE_NUMBER") or _text(ent, "DATAID") or name,
            entity_type="entity",
            primary_name=name,
            aliases=aliases,
            programs=[p for p in [_text(ent, "UN_LIST_TYPE")] if p],
            countries=_dedupe(countries),
            remarks=_text(ent, "COMMENTS1"),
        ))
    return records, version
