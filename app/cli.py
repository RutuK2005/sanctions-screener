"""Command-line entry points.

    python -m app.cli download            # fetch OFAC CSVs + UN XML into data/raw/
    python -m app.cli ingest-ofac data/raw
    python -m app.cli ingest-un data/raw/un_consolidated.xml
    python -m app.cli screen "Ayman al Zawahiri" --dob 1951-06-19
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import typer

from app.config import get_settings
from app.db import get_sessionmaker
from app.ingest.loader import load_records
from app.ingest.parsers import parse_ofac, parse_un
from app.matching.engine import screen as run_screen

cli = typer.Typer(add_completion=False, help="Sanctions screener utilities")

OFAC_BASE = "https://www.treasury.gov/ofac/downloads/"
UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"


@cli.command()
def download(dest: Path = Path("data/raw")) -> None:
    """Download the public OFAC SDN CSVs and UN consolidated XML (follows redirects)."""
    dest.mkdir(parents=True, exist_ok=True)
    targets = {f"{n}.csv": OFAC_BASE + f"{n}.csv" for n in ("sdn", "alt", "add")}
    targets["un_consolidated.xml"] = UN_URL
    for filename, url in targets.items():
        req = urllib.request.Request(url, headers={"User-Agent": "sanctions-screener/0.1"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        (dest / filename).write_bytes(data)
        typer.echo(f"{filename}: {len(data):,} bytes")


@cli.command("ingest-ofac")
def ingest_ofac(directory: Path) -> None:
    """Load sdn.csv/alt.csv/add.csv from DIRECTORY (idempotent)."""
    records, version = parse_ofac(directory)
    with get_sessionmaker()() as session:
        typer.echo(json.dumps(load_records(session, records, "OFAC_SDN", version)))


@cli.command("ingest-un")
def ingest_un(path: Path) -> None:
    """Load the UN consolidated list XML (idempotent)."""
    records, version = parse_un(path)
    with get_sessionmaker()() as session:
        typer.echo(json.dumps(load_records(session, records, "UN_SC", version)))


@cli.command()
def screen(
    name: str,
    entity_type: str = typer.Option(None, "--type", help="individual | entity"),
    dob: str = typer.Option(None, help="YYYY, YYYY-MM or YYYY-MM-DD"),
    country: str = typer.Option(None),
    explain: bool = typer.Option(False, help="print the full explanation JSON"),
) -> None:
    """Screen one name and print hits (no case is created)."""
    threshold = get_settings().match_threshold
    with get_sessionmaker()() as session:
        result = run_screen(session, name, entity_type=entity_type, dob=dob, country=country)
    typer.echo(f"normalised: {result.query_norm!r}  candidates: {result.candidates_considered}  "
               f"latency: {result.latency_ms:.1f} ms  threshold: {threshold}")
    for hit in result.hits:
        flag = "HIT " if hit.score >= threshold else "    "
        typer.echo(f"{flag}{hit.score:.3f}  {hit.primary_name}  [{hit.source} {hit.source_id}] "
                   f"via {hit.matched_name_type} {hit.matched_name!r}")
        if explain:
            typer.echo(json.dumps(hit.explanation, indent=2))


if __name__ == "__main__":
    cli()
