"""Analyst note for a case: LLM-written when ANTHROPIC_API_KEY is set, otherwise
(or on any API failure) a deterministic template built from the same evidence.

The LLM only *summarises* evidence the matcher already produced. It never
decides the case - the decision endpoint is human-only and audited.
"""

from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.schemas import CaseDetailOut

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are assisting a sanctions-screening analyst. Using ONLY the JSON evidence provided, write a "
    "concise case note (at most 120 words): who was screened, the strongest list match(es) with source "
    "and program, which features agreed or conflicted (name similarity, date of birth, country, type), "
    "and what the analyst should verify next. Do not recommend a final decision and do not invent facts."
)


def evidence(case: CaseDetailOut) -> dict:
    s = case.screening
    return {
        "query": {"name": s.query_name, "type": s.query_type, "dob": s.query_dob, "country": s.query_country},
        "threshold": s.threshold,
        "status": case.status,
        "hits": [
            {"listed_name": h.primary_name, "matched_alias": h.matched_name, "source": h.source,
             "source_id": h.source_id, "programs": h.programs, "type": h.entity_type, "score": h.score,
             "name_components": h.explanation.get("name"), "adjustments": h.explanation.get("adjustments")}
            for h in s.hits[:5]
        ],
    }


def template_summary(case: CaseDetailOut) -> str:
    s = case.screening
    if not s.hits:
        return f"Screening of '{s.query_name}' produced no hits at or above {s.threshold:.2f}."
    top = s.hits[0]
    adj = top.explanation.get("adjustments", {})
    lines = [
        f"'{s.query_name}' matched {len(s.hits)} listing(s) at or above threshold {s.threshold:.2f}.",
        f"Strongest: {top.primary_name} ({top.source} {top.source_id}; programs: "
        f"{', '.join(top.programs) or 'n/a'}) via alias '{top.matched_name}', score {top.score:.3f} "
        f"(name {top.explanation.get('name', {}).get('score', 0):.3f}).",
    ]
    for feature in ("dob", "country", "type"):
        if feature in adj:
            lines.append(f"{feature.upper()}: {adj[feature]['reason']} ({adj[feature]['value']:+.2f}).")
    if len(s.hits) > 1:
        others = "; ".join(f"{h.primary_name} ({h.score:.3f})" for h in s.hits[1:4])
        lines.append(f"Other hits: {others}.")
    lines.append("Verify identifiers (DOB, nationality, address, ID documents) against the listing before deciding.")
    return " ".join(lines)


def llm_summary(case: CaseDetailOut, api_key: str, model: str) -> str | None:
    """Return LLM text, or None if the call fails or is refused (caller falls back)."""
    import anthropic  # imported lazily: the template path must not need the SDK configured

    client = anthropic.Anthropic(api_key=api_key, timeout=30.0, max_retries=2)
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low"},
            # If the primary model declines, the API retries on a fallback model server-side.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": json.dumps(evidence(case), indent=2)}],
        )
    except anthropic.APIConnectionError as exc:
        log.warning("summary LLM unreachable: %s", exc)
        return None
    except anthropic.RateLimitError as exc:
        log.warning("summary LLM rate limited: %s", exc)
        return None
    except anthropic.APIStatusError as exc:
        log.warning("summary LLM error %s: %s", exc.status_code, exc)
        return None
    if response.stop_reason == "refusal":
        return None
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text or None


def generate_summary(case: CaseDetailOut) -> tuple[str, str]:
    settings = get_settings()
    if settings.anthropic_api_key:
        text = llm_summary(case, settings.anthropic_api_key, settings.summary_model)
        if text:
            return text, "llm"
    return template_summary(case), "template"
