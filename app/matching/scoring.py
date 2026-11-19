"""Re-scoring of retrieved candidates. Pure functions - no database access.

score = clamp( name_score + dob_adjustment + country_adjustment + type_adjustment , 0, 1 )

name_score = max(token_sort, token_alignment) where
  * token_sort       = rapidfuzz token_sort_ratio / 100 (edit distance after sorting tokens)
  * token_alignment  = 0.7 * query_coverage + 0.3 * candidate_coverage
      query_coverage     = length-weighted mean, over query tokens, of the best
                           Jaro-Winkler similarity against any candidate token
                           (a single-letter initial "a" matching "ayman" counts 0.9)
      candidate_coverage = the same in the other direction, unweighted (penalises
                           the list name having extra tokens, but only lightly -
                           middle names are often missing from customer data)
  Token pairs with Jaro-Winkler < 0.8 count as 0: below that it is shared
  letters, not a spelling variant.

Every number that goes into the final score is returned in `explanation` so an
analyst can see exactly why something matched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

INITIAL_MATCH = 0.9
JW_FLOOR = 0.8
PARTICLE_WEIGHT = 0.3

DOB_EXACT_BONUS = 0.05
DOB_YEAR_BONUS = 0.02
DOB_MISMATCH_PENALTY = -0.10
COUNTRY_MATCH_BONUS = 0.03
COUNTRY_MISMATCH_PENALTY = -0.05
TYPE_MISMATCH_PENALTY = -0.10


def _token_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) == 1 or len(b) == 1:  # initials
        return INITIAL_MATCH if a[0] == b[0] else 0.0
    sim = JaroWinkler.similarity(a, b)
    # Below ~0.8 Jaro-Winkler mostly reflects shared letters, not a spelling
    # variant ("calva" vs "carlos" = 0.76). Treat that as no match at all.
    return sim if sim >= JW_FLOOR else 0.0


def coverage(src: list[str], dst: list[str], *, length_weighted: bool = True) -> float:
    """How well every token of `src` is explained by some token of `dst` (0..1)."""
    if not src or not dst:
        return 0.0
    total_weight = 0.0
    acc = 0.0
    for tok in src:
        # Long tokens matter more than particles like "al" / "bin".
        if length_weighted:
            weight = max(len(tok), 2)
        else:  # every token counts the same, except 1-2 letter particles ("al", "el", "de")
            weight = 1.0 if len(tok) > 2 else PARTICLE_WEIGHT
        acc += weight * max(_token_sim(tok, other) for other in dst)
        total_weight += weight
    return acc / total_weight


@dataclass
class NameScore:
    score: float
    token_sort: float
    query_coverage: float
    candidate_coverage: float
    token_alignment: float


def name_similarity(query_norm: str, cand_norm: str) -> NameScore:
    q, c = query_norm.split(), cand_norm.split()
    ts = fuzz.token_sort_ratio(query_norm, cand_norm) / 100.0
    qc = coverage(q, c)
    # Unweighted on the list side: one long missing patronymic
    # ("anatolyevich") should not sink an otherwise perfect match.
    cc = coverage(c, q, length_weighted=False)
    align = 0.7 * qc + 0.3 * cc
    return NameScore(max(ts, align), ts, qc, cc, align)


# --------------------------------------------------------------------------- DOB

def parse_partial_date(value: str) -> tuple[int, int | None, int | None] | None:
    """'1951-06-19' -> (1951, 6, 19); '1951-06' -> (1951, 6, None); '1951' -> (1951, None, None)."""
    try:
        parts = [int(p) for p in value.split("-")]
    except (ValueError, AttributeError):
        return None
    if not parts or not 1000 <= parts[0] <= 2100:
        return None
    parts += [None] * (3 - len(parts))
    return parts[0], parts[1], parts[2]


def dob_adjustment(query_dob: str | None, list_dobs: list[str]) -> tuple[float, str]:
    if not query_dob or not list_dobs:
        return 0.0, "not compared (missing on one side)"
    q = parse_partial_date(query_dob)
    if q is None:
        return 0.0, "not compared (unparseable query DOB)"
    best = (DOB_MISMATCH_PENALTY, f"no listed DOB near {query_dob} (listed: {', '.join(list_dobs[:3])})")
    for raw in list_dobs:
        d = parse_partial_date(raw)
        if d is None:
            continue
        if d == q and d[2] is not None:
            return DOB_EXACT_BONUS, f"exact DOB match {raw}"
        if d[0] == q[0] and (d[1] is None or q[1] is None or d[1] == q[1]):
            best = max(best, (DOB_YEAR_BONUS, f"year-level DOB match {raw}"))
        elif abs(d[0] - q[0]) <= 1:
            best = max(best, (0.0, f"DOB within one year ({raw})"))
    return best


# ----------------------------------------------------------------------- country

def _norm_country(value: str) -> str:
    return value.strip().casefold()


def country_adjustment(query_country: str | None, list_countries: list[str]) -> tuple[float, str]:
    if not query_country or not list_countries:
        return 0.0, "not compared (missing on one side)"
    q = _norm_country(query_country)
    listed = {_norm_country(c) for c in list_countries}
    if q in listed:
        return COUNTRY_MATCH_BONUS, f"country {query_country} is linked to the listing"
    return COUNTRY_MISMATCH_PENALTY, f"country {query_country} not among {sorted(listed)[:4]}"


def type_adjustment(query_type: str | None, entity_type: str) -> tuple[float, str]:
    if not query_type:
        return 0.0, "not compared"
    if query_type == entity_type or (query_type == "entity" and entity_type in ("vessel", "aircraft")):
        return 0.0, "type agrees"
    return TYPE_MISMATCH_PENALTY, f"query is {query_type}, listing is {entity_type}"


# ------------------------------------------------------------------------- total

@dataclass
class ScoredCandidate:
    entity_id: int
    matched_name: str
    score: float
    explanation: dict = field(default_factory=dict)


def score_candidate(
    *,
    query_norm: str,
    cand_norm: str,
    query_dob: str | None,
    query_country: str | None,
    query_type: str | None,
    entity_type: str,
    list_dobs: list[str],
    list_countries: list[str],
    trigram_similarity: float | None = None,
) -> tuple[float, dict]:
    ns = name_similarity(query_norm, cand_norm)
    dob_adj, dob_reason = dob_adjustment(query_dob, list_dobs)
    ctry_adj, ctry_reason = country_adjustment(query_country, list_countries)
    type_adj, type_reason = type_adjustment(query_type, entity_type)
    total = min(1.0, max(0.0, ns.score + dob_adj + ctry_adj + type_adj))
    explanation = {
        "query_normalized": query_norm,
        "candidate_normalized": cand_norm,
        "name": {
            "score": round(ns.score, 4),
            "token_sort_ratio": round(ns.token_sort, 4),
            "query_coverage_jw": round(ns.query_coverage, 4),
            "candidate_coverage_jw": round(ns.candidate_coverage, 4),
            "token_alignment": round(ns.token_alignment, 4),
            "trigram_similarity": None if trigram_similarity is None else round(trigram_similarity, 4),
        },
        "adjustments": {
            "dob": {"value": dob_adj, "reason": dob_reason},
            "country": {"value": ctry_adj, "reason": ctry_reason},
            "type": {"value": type_adj, "reason": type_reason},
        },
        "final_score": round(total, 4),
    }
    return total, explanation
