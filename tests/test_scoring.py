import pytest

from app.matching.scoring import (
    COUNTRY_MATCH_BONUS,
    COUNTRY_MISMATCH_PENALTY,
    DOB_EXACT_BONUS,
    DOB_MISMATCH_PENALTY,
    DOB_YEAR_BONUS,
    TYPE_MISMATCH_PENALTY,
    country_adjustment,
    coverage,
    dob_adjustment,
    name_similarity,
    parse_partial_date,
    score_candidate,
    type_adjustment,
)


def test_identical_names_score_one():
    assert name_similarity("ayman al zawahiri", "ayman al zawahiri").score == 1.0


def test_reordered_tokens_score_one():
    ns = name_similarity("zawahiri ayman", "ayman zawahiri")
    assert ns.token_sort == 1.0 and ns.score == 1.0


def test_initial_counts_as_partial_match():
    ns = name_similarity("a zawahiri", "ayman zawahiri")
    assert 0.9 <= ns.query_coverage < 1.0
    assert ns.score > 0.9


def test_dropped_middle_name_still_high():
    ns = name_similarity("sergey tsyb", "sergey anatolyevich tsyb")
    assert ns.query_coverage == 1.0
    assert ns.score >= 0.88


def test_unrelated_tokens_get_zero_credit():
    # "calva" vs "carlos" share letters but are not spelling variants (JW < 0.8)
    assert coverage(["calva"], ["carlos"]) == 0.0


def test_typo_scores_between():
    ns = name_similarity("aiman zawahri", "ayman zawahiri")
    assert 0.85 < ns.score < 1.0


def test_unrelated_names_low():
    assert name_similarity("john smith", "banco nacional de cuba").score < 0.5


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1951-06-19", (1951, 6, 19)), ("1951-06", (1951, 6, None)), ("1951", (1951, None, None)),
     ("garbage", None), ("0001", None)],
)
def test_parse_partial_date(value, expected):
    assert parse_partial_date(value) == expected


def test_dob_adjustments():
    assert dob_adjustment("1951-06-19", ["1951-06-19"])[0] == DOB_EXACT_BONUS
    assert dob_adjustment("1951-06-19", ["1951"])[0] == DOB_YEAR_BONUS
    assert dob_adjustment("1951", ["1951-06-19"])[0] == DOB_YEAR_BONUS
    assert dob_adjustment("1952-01-01", ["1951-06-19"])[0] == 0.0  # within a year: neutral
    assert dob_adjustment("1980-01-01", ["1951-06-19"])[0] == DOB_MISMATCH_PENALTY
    assert dob_adjustment(None, ["1951"])[0] == 0.0
    assert dob_adjustment("1951", [])[0] == 0.0


def test_country_and_type_adjustments():
    assert country_adjustment("egypt", ["Egypt"])[0] == COUNTRY_MATCH_BONUS
    assert country_adjustment("India", ["Egypt"])[0] == COUNTRY_MISMATCH_PENALTY
    assert country_adjustment(None, ["Egypt"])[0] == 0.0
    assert type_adjustment("individual", "entity")[0] == TYPE_MISMATCH_PENALTY
    assert type_adjustment("entity", "vessel")[0] == 0.0
    assert type_adjustment(None, "entity")[0] == 0.0


def test_score_candidate_explains_every_component():
    score, exp = score_candidate(
        query_norm="ayman zawahiri", cand_norm="ayman al zawahiri", query_dob="1951-06-19",
        query_country="Egypt", query_type="individual", entity_type="individual",
        list_dobs=["1951-06-19"], list_countries=["Egypt"], trigram_similarity=0.7,
    )
    name = exp["name"]["score"]
    assert score == pytest.approx(min(1.0, name + DOB_EXACT_BONUS + COUNTRY_MATCH_BONUS))
    assert exp["adjustments"]["dob"]["value"] == DOB_EXACT_BONUS
    assert exp["adjustments"]["country"]["value"] == COUNTRY_MATCH_BONUS
    assert set(exp["name"]) >= {"token_sort_ratio", "query_coverage_jw", "candidate_coverage_jw", "trigram_similarity"}
    assert exp["final_score"] == round(score, 4)


def test_score_is_clamped_to_one():
    score, _ = score_candidate(query_norm="ayman al zawahiri", cand_norm="ayman al zawahiri", query_dob="1951-06-19",
                               query_country="Egypt", query_type="individual", entity_type="individual",
                               list_dobs=["1951-06-19"], list_countries=["Egypt"])
    assert score == 1.0


def test_penalties_reduce_score():
    base, _ = score_candidate(query_norm="ayman zawahiri", cand_norm="ayman al zawahiri", query_dob=None,
                              query_country=None, query_type=None, entity_type="individual",
                              list_dobs=["1951-06-19"], list_countries=["Egypt"])
    worse, _ = score_candidate(query_norm="ayman zawahiri", cand_norm="ayman al zawahiri", query_dob="1990-01-01",
                               query_country="India", query_type="entity", entity_type="individual",
                               list_dobs=["1951-06-19"], list_countries=["Egypt"])
    expected = base + DOB_MISMATCH_PENALTY + COUNTRY_MISMATCH_PENALTY + TYPE_MISMATCH_PENALTY
    assert worse == pytest.approx(expected)
