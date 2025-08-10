import pytest
from main import _decide_intent, _get_state, _clear_state, INTENT_PHARMACY, INTENT_INFO

def test_sticky_pharmacy_with_paralia():
    sid = "u1"
    _clear_state(sid)
    # ξεκινάμε με PHARMACY
    state = _get_state(sid)
    state.intent = INTENT_PHARMACY
    state.slots = {"area": None}
    # ο χρήστης γράφει "παραλια"
    new_intent = _decide_intent(sid, "παραλια", None, 0.0)
    assert new_intent == INTENT_PHARMACY  # δεν αλλάζει σε INFO

def test_trip_time_range_not_trip_intent():
    sid = "u2"
    _clear_state(sid)
    txt = "02:00 - 08:30 ΑΓΓΕΛΟΠΟΥΛΟΥ ΕΦΗ"
    intent = _decide_intent(sid, txt, None, 0.0)
    assert intent != "TripCostIntent"
