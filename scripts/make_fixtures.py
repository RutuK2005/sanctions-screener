"""Cut a small, deterministic subset of the real lists into tests/fixtures/ so
tests and CI run offline. Rows are copied verbatim (same format as the source).

    python -I scripts/make_fixtures.py data/raw tests/fixtures
"""

from __future__ import annotations

import csv
import io
import random
import sys
from pathlib import Path

from defusedxml import ElementTree as DET

ALWAYS = {"2676", "306", "2674", "4632", "173", "36"}  # well-known rows the tests reference
N_OFAC = 300
N_UN_IND, N_UN_ENT = 20, 10


def read_rows(path: Path) -> list[list[str]]:
    text = path.read_bytes().decode("utf-8", errors="replace")
    return [r for r in csv.reader(io.StringIO(text)) if len(r) >= 2 and r[0].strip().isdigit()]


def write_rows(path: Path, rows: list[list[str]]) -> None:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8")


def main(src: Path, dst: Path) -> None:
    rng = random.Random(42)
    sdn = read_rows(src / "sdn.csv")
    ids = sorted({r[0].strip() for r in sdn})
    picked = set(ALWAYS) | set(rng.sample([i for i in ids if i not in ALWAYS], N_OFAC - len(ALWAYS)))
    out = dst / "ofac"
    out.mkdir(parents=True, exist_ok=True)
    for name in ("sdn.csv", "alt.csv", "add.csv"):
        rows = [r for r in read_rows(src / name) if r[0].strip() in picked]
        write_rows(out / name, rows)
        print(name, len(rows))

    tree = DET.parse(str(src / "un_consolidated.xml"))
    root = tree.getroot()
    for group, child, keep in (("INDIVIDUALS", "INDIVIDUAL", N_UN_IND), ("ENTITIES", "ENTITY", N_UN_ENT)):
        parent = root.find(group)
        items = parent.findall(child)
        for el in items[keep:]:
            parent.remove(el)
    tree.write(dst / "un_sample.xml", encoding="UTF-8", xml_declaration=True)
    print("un_sample.xml", N_UN_IND, "individuals,", N_UN_ENT, "entities")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
