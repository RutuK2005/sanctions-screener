"""Name normalisation: turn messy human input into a comparable key.

normalize("Dr. Ayman AL-ZAWAHIRI")   -> "ayman al zawahiri"
normalize("ZAWAHIRI, Ayman")         -> "ayman zawahiri"   (\"LAST, First\" reordered)
normalize("Société Générale S.A.")   -> "societe generale"  (diacritics + legal form dropped)

The same function is applied to list names at ingest time and to the query at
screening time, so both sides live in the same "normalised space".
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz.distance import OSA

HONORIFICS = frozenset(
    """
        mr mrs ms miss mx dr prof sir dame lord lady rev hon sheikh shaykh sheik haji hajji hajj mullah
        maulana mawlana imam gen general col colonel maj major capt captain lt sgt adm admiral herr frau mme
        mlle sr sra
    """.split()
)

# Legal-form suffixes carry no identifying signal ("Acme Ltd" == "Acme Limited").
LEGAL_FORMS = frozenset(
    """
        ltd limited llc inc incorporated corp corporation co company plc sa ag gmbh bv nv srl spa sarl sas
        jsc ojsc pjsc cjsc ooo zao oao llp lp pte pvt fze fzco fzc kft the liability joint stock
        aktsionernoe obshchestvo otkrytoe zakrytoe publichnoe
    """.split()
)

# Multi-word legal forms that contain ordinary words ("de"), so they are only
# removed as a whole sequence (longest first).
LEGAL_PHRASES = sorted(
    (tuple(p.split()) for p in ("s de rl de cv", "s de rl", "sa de cv", "de cv", "general trading",
                                "s ogranichennoi otvetstvennostyu", "s ogranichennoy otvetstvennostyu")),
    key=len, reverse=True,
)


def _strip_phrases(toks: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(toks):
        for phrase in LEGAL_PHRASES:
            if tuple(toks[i : i + len(phrase)]) == phrase:
                i += len(phrase)
                break
        else:
            out.append(toks[i])
            i += 1
    return out

# A deliberately tiny canonicalisation table for the single most common
# transliteration family on sanctions lists. Larger tables would need care:
# every entry makes two different real names collide.
CANONICAL_TOKENS = {
    v: "muhammad"
    for v in ("mohammed", "mohamed", "mohammad", "muhammed", "mohamad", "muhamad", "mohd", "mohammed", "muhammet")
}

# Long legal-form words are also stripped when misspelt by one edit or one
# transposition ("limted", "lmiited", "liabiliti"), so a typo in the boring part
# of a company name doesn't leave a stray token on only one side.
_FUZZY_LEGAL_FORMS = tuple(f for f in LEGAL_FORMS if len(f) >= 6)


def _is_legal_form(token: str) -> bool:
    if token in LEGAL_FORMS:
        return True
    return len(token) >= 6 and any(OSA.distance(token, form, score_cutoff=1) <= 1
                                   for form in _FUZZY_LEGAL_FORMS)


_DOTTED_ACRONYM = re.compile(r"\b(?:[^\W\d_]\.)+[^\W\d_]\b\.?")  # L.L.C. / S.A. / R.L.
_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_APOSTROPHES = re.compile(r"['’‘`ʼ]")
_SPACES = re.compile(r"\s+")


def strip_diacritics(text: str) -> str:
    """'Müller' -> 'Muller', 'Ñandú' -> 'Nandu' (NFKD then drop combining marks)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _clean(fragment: str) -> list[str]:
    fragment = strip_diacritics(fragment).casefold()
    fragment = _DOTTED_ACRONYM.sub(lambda m: m[0].replace(".", ""), fragment)  # "l.l.c." -> "llc"
    fragment = _APOSTROPHES.sub("", fragment)  # O'Brien -> obrien, not "o brien"
    fragment = _PUNCT.sub(" ", fragment)  # hyphens, dots, slashes -> space
    fragment = fragment.replace("_", " ")
    return [t for t in _SPACES.split(fragment) if t]


def tokens(name: str) -> list[str]:
    """Normalised tokens in reading order, with honorifics/legal forms removed."""
    if not name:
        return []
    # "LAST, First Middle" -> "First Middle LAST" (only for a single comma whose
    # tail is not a legal form, so "Acme Trading, Ltd." is left alone).
    parts = name.split(",")
    if len(parts) == 2:
        head, tail = _clean(parts[0]), _clean(parts[1])
        if tail and not all(_is_legal_form(t) for t in tail):
            raw = tail + head
        else:
            raw = head + tail
    else:
        raw = [t for part in parts for t in _clean(part)]

    out = [CANONICAL_TOKENS.get(t, t) for t in raw if t not in HONORIFICS]
    stripped = [t for t in _strip_phrases(out) if not _is_legal_form(t)]
    # Never strip a name down to nothing ("The Company" stays "the company").
    return stripped or out


def normalize(name: str) -> str:
    return " ".join(tokens(name))


def sorted_key(name_norm: str) -> str:
    """Order-insensitive key: 'zawahiri ayman' and 'ayman zawahiri' collide."""
    return " ".join(sorted(name_norm.split()))
