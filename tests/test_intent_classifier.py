import pytest
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from intents import IntentClassifier, extract_entities
from session_manager import SessionManager


def test_patras_fills_origin_without_changing_intent():
    classifier = IntentClassifier()
    sess_mgr = SessionManager()
    user_id = "user1"

    # Pretend destination already provided; origin is missing
    sess_mgr.update_slot(user_id, "TripCostIntent", "destination", "Αθήνα")
    sess_mgr.set_active_intent(user_id, "TripCostIntent")

    last_intent = sess_mgr.get_last_intent(user_id)
    missing = sess_mgr.get_missing_slots(user_id, last_intent)
    assert missing == ["origin"]

    result = classifier.detect(
        "πατρα", active_intent=last_intent, missing_slots=missing
    )
    # Boost should be skipped so intent remains default
    assert result["intent"] == "default"

    # Mimic context patching to fill the slot
    slot_to_fill = missing[0]
    extracted = extract_entities("πατρα")
    value = extracted.get(slot_to_fill) or "πατρα"
    sess_mgr.update_slot(user_id, last_intent, slot_to_fill, value)
    for k, v in extracted.items():
        if k != slot_to_fill:
            sess_mgr.update_slot(user_id, last_intent, k, v)

    slots = sess_mgr.get_active_slots(user_id, last_intent)
    assert slots["origin"].lower() == "πατρα"
    # Ensure intent stays the same
    assert sess_mgr.get_last_intent(user_id) == "TripCostIntent"
    # All required slots should now be filled
    assert not sess_mgr.get_missing_slots(user_id, last_intent)