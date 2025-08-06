import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from intents import extract_entities


def test_unaccented_range():
    result = extract_entities("απο πατρα μεχρι αθηνα")
    assert result == {"FROM": "Πατρα", "TO": "Αθηνα"}


def test_uppercase_range():
    result = extract_entities("ΑΠΟ ΠΑΤΡΑ ΜΕΧΡΙ ΑΘΗΝΑ")
    assert result["FROM"] == "Πατρα"
    assert result["TO"] == "Αθηνα"


def test_unaccented_eos():
    result = extract_entities("απο πατρα εως αθηνα")
    assert result == {"FROM": "Πατρα", "TO": "Αθηνα"}