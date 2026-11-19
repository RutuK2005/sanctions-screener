import pytest

from app.matching.normalize import normalize, sorted_key, strip_diacritics, tokens


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AL ZAWAHIRI, Dr. Ayman", "ayman al zawahiri"),  # LAST, First + honorific
        ("Dr. Ayman AL-ZAWAHIRI", "ayman al zawahiri"),  # hyphen -> space
        ("  ayman   al zawahiri ", "ayman al zawahiri"),  # whitespace
        ("Société Générale S.A.", "societe generale"),  # diacritics + dotted legal form
        ("O'Brien, Seán", "sean obrien"),  # apostrophe removed, not split
        ("Mohammed Ali", "muhammad ali"),  # transliteration family
        ("MOHAMED Ali", "muhammad ali"),
        ("Acme Trading, Ltd.", "acme trading"),  # comma before a legal form: no reorder
        ("Nord Trading L.L.C", "nord trading"),
        ("Tecnologia Optima S. de R.L. de C.V.", "tecnologia optima"),
        ("Limited Liability Company Armeks", "armeks"),
        ("Lmiited Liability Compnay Armeks", "armeks"),  # one-typo legal forms still stripped
        ("Banco Nacional de Cuba", "banco nacional de cuba"),  # "de" alone is kept
        ("The Company", "the company"),  # never strip to nothing
        ("", ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_strip_diacritics():
    assert strip_diacritics("Müller Ñandú Øre") == "Muller Nandu Øre"  # Ø has no decomposition


def test_order_insensitive_key():
    assert sorted_key(normalize("Zawahiri, Ayman")) == sorted_key(normalize("Ayman Zawahiri"))


def test_tokens_drop_honorifics():
    assert tokens("Sheikh Mr. Omar Abdel Rahman") == ["omar", "abdel", "rahman"]
