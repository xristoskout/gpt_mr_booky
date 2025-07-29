# tests/test_nlp_utils.py

import pytest
from nlp_utils import normalize_text, strip_article, extract_area


def test_strip_accents_and_normalize():
    s = "Πάτρα, Ρίο! Βραχνέικα; Μεσσάτιδα."
    norm = normalize_text(s)
    assert "πατρα" in norm
    assert "ριο" in norm
    assert "βραχνεικα" in norm
    assert "μεσασιτιδα" not in norm  # typo‐check example


def test_strip_article():
    assert strip_article("Ο Πάτρας") == "Πάτρας"
    assert strip_article("τη Μεσσάτιδα") == "Μεσσάτιδα"


@pytest.mark.parametrize("inp,expected", [
    ("πατρα", "Πάτρα"),
    ("ριον", "Ρίο"),
    ("ΠαΡαλια", "Παραλία Πατρών"),
])
def test_extract_area_known(inp, expected):
    assert extract_area(inp) == expected


def test_extract_area_unknown():
    assert extract_area("αθηνα") is None
