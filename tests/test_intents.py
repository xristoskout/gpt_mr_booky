# tests/test_intents.py

import pytest
from intents import IntentClassifier
from pathlib import Path


@pytest.fixture
def classifier():
    # Path to intents.json in project root
    intents_path = Path(__file__).parent.parent / "intents.json"
    return IntentClassifier(intents_path, fuzzy_threshold=0.75)


def test_keyword_contact(classifier):
    msg = "πώς κατεβάζω το app;"
    result = classifier.detect(msg)
    assert result["intent"] == "ContactInfoIntent"


def test_keyword_trip_cost(classifier):
    for txt in ["πόσο κοστίζει", "πόσα χλμ", "κόστος διαδρομής"]:
        result = classifier.detect(txt)
        assert result["intent"] == "TripCostIntent"


def test_fuzzy_pharmacy(classifier):
    msg = "πού είναι φαρμακείο διανυκτερεύων;"
    result = classifier.detect(msg)
    assert result["intent"] == "OnDutyPharmacyIntent"