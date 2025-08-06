import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from session_manager import SessionManager


def test_clear_slots_removes_intent_data():
    sm = SessionManager()
    uid = 'user'
    sm.update_slot(uid, 'TripCostIntent', 'origin', 'Patra')
    sm.update_slot(uid, 'TripCostIntent', 'destination', 'Athina')
    assert sm.get_missing_slots(uid, 'TripCostIntent') == []
    sm.clear_slots(uid, 'TripCostIntent')
    assert sm.get_missing_slots(uid, 'TripCostIntent') == ['origin', 'destination']