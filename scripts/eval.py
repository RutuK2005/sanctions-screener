"""Offline evaluation of the matcher against whatever list is loaded in DATABASE_URL.

    DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:54329/screener_eval \
        python scripts/eval.py --positives 1000 --negatives 1000 --out docs/eval-results.json

Labelled set (generated, seeded, reproducible):
  positives - real names from the loaded list, perturbed the way customer data is
              messy: typo, transliteration-style spelling swap, token reorder,
              dropped middle name, first name -> initial. Expected: an alert that
              includes the source entity.
  negatives - random first+last combinations of common names from several regions
              plus generic company names, none of which equal a listed name after
              normalisation. Expected: no alert.

An "alert" means at least one hit with score >= threshold. A positive counts as a
true positive only if the *correct* entity is among the alerted hits.
Latency is wall-clock time of engine.screen() (SQL retrieval + Python re-scoring)
on a warm connection, measured per query.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import get_sessionmaker  # noqa: E402
from app.matching.engine import screen  # noqa: E402
from app.matching.normalize import normalize, tokens  # noqa: E402

THRESHOLDS = [0.75, 0.80, 0.85, 0.88, 0.90, 0.92, 0.95]

FIRST_NAMES = """
    james mary robert patricia michael jennifer william linda david elizabeth richard barbara joseph
    susan thomas jessica charles sarah christopher karen daniel nancy matthew lisa anthony betty mark
    sandra donald ashley rahul priya amit sneha vikram anjali arjun pooja rohan kavya sanjay neha aditya
    divya karthik lakshmi suresh deepa rajesh meera wei fang jun li ming hui xiao yan hiroshi yuki kenji
    akiko takeshi sakura minjun seoyeon jose maria juan carmen luis ana carlos lucia miguel sofia javier
    elena diego paula pablo marta olga ivan anna sergei natalia dmitri elena pavel irina andrei tatiana
    nikolai omar fatima yusuf aisha hassan layla khalid zainab tariq amina karim noor bilal samira
    faisal hana emeka ngozi chinedu adaeze kwame ama kofi abena tunde funmilayo lukas emma leon mia finn
    hannah jan sophie lucas chloe hugo louise
""".split()

LAST_NAMES = """
    smith johnson williams brown jones garcia miller davis rodriguez martinez hernandez lopez gonzalez
    wilson anderson thomas taylor moore jackson martin lee thompson white harris clark lewis robinson
    walker young allen king wright sharma patel singh kumar gupta reddy iyer nair menon das bose
    chatterjee mehta joshi verma rao pillai desai wang zhang liu chen yang huang zhao wu zhou xu sato
    suzuki takahashi tanaka watanabe kim park choi jung kang fernandez torres ramirez flores rivera
    gomez diaz cruz morales ortiz gutierrez castillo vargas romero ivanov smirnov kuznetsov popov
    sokolov lebedev kozlov novikov morozov volkov haddad nasser saleh mansour khoury aziz rahman karimi
    hosseini farouk qureshi siddiqui chaudhry malik okafor okonkwo adeyemi mensah boateng owusu nwosu
    eze balogun muller schmidt schneider fischer weber meyer wagner becker schulz hoffmann dubois
    laurent bernard moreau
""".split()

COMPANY_WORDS_A = """
    green blue northern pacific golden silver summit river bright prime urban global coastal royal
""".split()
COMPANY_WORDS_B = """
    valley harbor peak bridge stone oak maple lake field ridge star leaf wave forest
""".split()
COMPANY_WORDS_C = ("trading", "logistics", "foods", "textiles", "software", "consulting", "pharma", "motors",
                   "realty", "ventures", "solutions", "industries")
COMPANY_SUFFIX = ("ltd", "llc", "inc", "pvt ltd", "gmbh", "", "")

TRANSLIT_RULES = [("ou", "u"), ("u", "ou"), ("kh", "h"), ("sh", "ch"), ("y", "i"), ("i", "y"), ("ee", "i"),
                  ("ph", "f"), ("q", "k"), ("k", "q"), ("j", "dj"), ("ov", "off"), ("ev", "eff"), ("w", "v"),
                  ("dh", "d"), ("th", "t"), ("c", "k"), ("z", "s"), ("ei", "ai"), ("oo", "u")]


def display_name(name: str) -> str:
    """Turn 'LAST, First Middle' into how a customer would type it: 'First Middle Last'."""
    parts = name.split(",")
    if len(parts) == 2 and parts[1].strip():
        name = f"{parts[1].strip()} {parts[0].strip()}"
    return " ".join(w.capitalize() if w.isupper() else w for w in name.split())


def typo(rng: random.Random, word: str) -> str:
    if len(word) < 4:
        return word
    i = rng.randrange(1, len(word) - 1)
    op = rng.choice(["swap", "delete", "insert", "replace"])
    letters = "abcdefghijklmnopqrstuvwxyz"
    if op == "swap":
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    if op == "delete":
        return word[:i] + word[i + 1:]
    if op == "insert":
        return word[:i] + rng.choice(letters) + word[i:]
    return word[:i] + rng.choice(letters) + word[i + 1:]


def make_variant(rng: random.Random, name: str, entity_type: str) -> tuple[str, str] | None:
    words = display_name(name).split()
    if not words:
        return None
    long_idx = [i for i, w in enumerate(words) if len(w) >= 4]
    kinds = ["typo", "translit", "reorder"]
    if entity_type == "individual" and len(words) >= 3:
        kinds.append("drop_middle")
    if entity_type == "individual" and len(words) >= 2 and len(words[-1]) >= 4:
        kinds.append("initial")
    rng.shuffle(kinds)
    for kind in kinds:
        w = list(words)
        if kind == "typo" and long_idx:
            i = rng.choice(long_idx)
            w[i] = typo(rng, w[i])
        elif kind == "translit":
            options = [(i, a, b) for i, word in enumerate(w) for a, b in TRANSLIT_RULES if a in word.lower()]
            if not options:
                continue
            i, a, b = rng.choice(options)
            w[i] = w[i].lower().replace(a, b, 1).capitalize()
        elif kind == "reorder" and len(w) >= 2:
            w = w[1:] + w[:1]
        elif kind == "drop_middle":
            del w[rng.randrange(1, len(w) - 1)]
        elif kind == "initial":
            w[0] = w[0][0] + "."
        else:
            continue
        variant = " ".join(w)
        if normalize(variant) != normalize(name) or kind == "reorder":
            return variant, kind
    return None


def build_dataset(session, n_pos: int, n_neg: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    rows = session.execute(text(
        "SELECT id, entity_type, primary_name, "
        "(SELECT array_agg(name) FROM entity_names n WHERE n.entity_id = e.id) AS names "
        "FROM sanctioned_entities e WHERE active AND entity_type IN ('individual', 'entity') ORDER BY id"
    )).all()
    listed_norms = {r[0] for r in session.execute(text("SELECT DISTINCT name_norm FROM entity_names"))}

    dataset: list[dict] = []
    for entity_id, entity_type, primary_name, names in rng.sample(rows, min(len(rows), n_pos * 2)):
        if len(dataset) >= n_pos:
            break
        source_name = rng.choice(names) if rng.random() < 0.3 and names else primary_name
        if len(tokens(source_name)) < 1:
            continue
        made = make_variant(rng, source_name, entity_type)
        if not made:
            continue
        query, kind = made
        dataset.append({"query": query, "type": entity_type, "label": 1, "kind": kind,
                        "entity_id": entity_id, "source_name": source_name})

    negatives: list[dict] = []
    while len(negatives) < n_neg:
        if rng.random() < 0.75:
            q = f"{rng.choice(FIRST_NAMES).capitalize()} {rng.choice(LAST_NAMES).capitalize()}"
            etype = "individual"
        else:
            words = [rng.choice(COMPANY_WORDS_A), rng.choice(COMPANY_WORDS_B), rng.choice(COMPANY_WORDS_C)]
            q = " ".join(w.capitalize() for w in words)
            if suffix := rng.choice(COMPANY_SUFFIX):
                q += " " + suffix.upper()
            etype = "entity"
        if normalize(q) in listed_norms or any(n["query"] == q for n in negatives):
            continue
        negatives.append({"query": q, "type": etype, "label": 0, "kind": "negative_" + etype})
    return dataset + negatives


def percentile(values: list[float], p: float) -> float:
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def prf(items: list[dict], score_key: str, correct_key: str, t: float) -> dict:
    tp = sum(1 for x in items if x["label"] == 1 and x[correct_key] >= t)
    fn = sum(1 for x in items if x["label"] == 1) - tp
    fp = sum(1 for x in items if x["label"] == 0 and x[score_key] >= t)
    # A positive that alerts only on *wrong* entities is also a false alert.
    fp += sum(1 for x in items if x["label"] == 1 and x[correct_key] < t and x[score_key] >= t)
    tn = sum(1 for x in items if x["label"] == 0) - sum(1 for x in items if x["label"] == 0 and x[score_key] >= t)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    negatives = sum(1 for x in items if x["label"] == 0)
    return {"threshold": t, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": round(precision, 4),
            "recall": round(recall, 4), "f1": round(f1, 4),
            "negative_alert_rate": round((negatives - tn) / max(1, negatives), 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--positives", type=int, default=1000)
    ap.add_argument("--negatives", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--show-errors", type=int, default=0)
    args = ap.parse_args()

    session = get_sessionmaker()()
    n_entities = session.execute(text("SELECT count(*) FROM sanctioned_entities WHERE active")).scalar_one()
    n_names = session.execute(text("SELECT count(*) FROM entity_names")).scalar_one()
    sources = [r[0] for r in session.execute(text("SELECT DISTINCT source FROM sanctioned_entities WHERE active"))]
    dataset = build_dataset(session, args.positives, args.negatives, args.seed)

    screen(session, "warm up query")  # first query pays connection + plan cache costs
    latencies = []
    for item in dataset:
        t0 = time.perf_counter()
        result = screen(session, item["query"], entity_type=item["type"], max_results=50)
        latencies.append((time.perf_counter() - t0) * 1000)
        session.rollback()  # end the read transaction; keeps each query independent
        item["top_score"] = result.hits[0].score if result.hits else 0.0
        item["top_trgm"] = max((h.trigram for h in result.hits), default=0.0)
        correct = [h for h in result.hits if h.entity_id == item.get("entity_id")]
        item["correct_score"] = correct[0].score if correct else 0.0
        item["correct_trgm"] = correct[0].trigram if correct else 0.0
        item["top_hit"] = result.hits[0].primary_name if result.hits else None

    rescored = [prf(dataset, "top_score", "correct_score", t) for t in THRESHOLDS]
    baseline = [prf(dataset, "top_trgm", "correct_trgm", t) for t in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)]
    best = max(rescored, key=lambda r: r["f1"])

    by_kind: dict[str, dict] = {}
    for kind in sorted({x["kind"] for x in dataset}):
        sub = [x for x in dataset if x["kind"] == kind]
        if kind.startswith("negative"):
            alerts = sum(1 for x in sub if x["top_score"] >= best["threshold"])
            by_kind[kind] = {"n": len(sub), "alert_rate": round(alerts / len(sub), 4)}
        else:
            found = sum(1 for x in sub if x["correct_score"] >= best["threshold"])
            by_kind[kind] = {"n": len(sub), "recall": round(found / len(sub), 4)}

    report = {
        "list": {"sources": sources, "active_entities": n_entities, "names_incl_aliases": n_names},
        "dataset": {"positives": sum(x["label"] for x in dataset),
                    "negatives": sum(1 - x["label"] for x in dataset), "seed": args.seed},
        "rescored": rescored,
        "baseline_trigram_only": baseline,
        "best_f1_threshold": best["threshold"],
        "by_kind_at_best": by_kind,
        "latency_ms": {"n": len(latencies), "p50": round(percentile(latencies, 0.50), 2),
                       "p95": round(percentile(latencies, 0.95), 2), "p99": round(percentile(latencies, 0.99), 2),
                       "mean": round(statistics.mean(latencies), 2)},
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "postgres": session.execute(text("SHOW server_version")).scalar_one()},
    }

    print(f"list: {sources} active entities={n_entities} names={n_names}")
    print(f"dataset: {report['dataset']}")
    print("\nre-scored matcher")
    print("| threshold | precision | recall | F1 | TP | FP | FN | negative alert rate |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rescored:
        print(f"| {r['threshold']:.2f} | {r['precision']:.3f} | {r['recall']:.3f} | {r['f1']:.3f} | {r['tp']} | "
              f"{r['fp']} | {r['fn']} | {r['negative_alert_rate']:.3f} |")
    print("\nbaseline: raw pg_trgm similarity only")
    for r in baseline:
        print(f"| {r['threshold']:.2f} | {r['precision']:.3f} | {r['recall']:.3f} | {r['f1']:.3f} |")
    print(f"\nby variant kind at {best['threshold']}: {json.dumps(by_kind)}")
    print(f"latency: {report['latency_ms']}")

    if args.show_errors:
        t = best["threshold"]
        misses = [x for x in dataset if x["label"] == 1 and x["correct_score"] < t]
        false_alerts = [x for x in dataset if x["label"] == 0 and x["top_score"] >= t]
        print("\nmissed positives:")
        for x in misses[: args.show_errors]:
            print(f"  [{x['kind']}] {x['query']!r} <- {x['source_name']!r} "
                  f"correct={x['correct_score']:.3f} top={x['top_hit']!r}")
        print("false alerts on negatives:")
        for x in false_alerts[: args.show_errors]:
            print(f"  {x['query']!r} -> {x['top_hit']!r} {x['top_score']:.3f}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
