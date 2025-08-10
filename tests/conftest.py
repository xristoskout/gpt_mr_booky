# tests/conftest.py
import pytest
from fastapi.testclient import TestClient

import tools as tools_mod
import main as main_mod

# --- Fakes για APIs ---

class FakePharmacyClient:
    def __init__(self): pass
    def get_on_duty(self, area: str = "Πάτρα", method: str = "get"):
        # Γύρνα 1-2 φαρμακεία για να ελέγξουμε formatting
        if "ζαρουχ" in area.lower():
            return []  # να δούμε το "none" flow
        return [
            {"name": "Φαρμακείο Α", "address": f"{area} - Οδός 1", "time_range": "08:00 - 21:00"},
            {"name": "Φαρμακείο Β", "address": f"{area} - Οδός 2", "time_range": "21:00 - 08:00"},
        ]

class FakeTimologioClient:
    def __init__(self): pass
    def estimate_trip(self, origin: str, destination: str, when: str = "now"):
        # Σταθερό, προβλέψιμο payload
        return {
            "price_eur": 268.0,
            "distance_km": 211.0,
            "duration_min": 140,
            "map_url": "https://www.google.com/maps/dir/?api=1&origin=patra&destination=athens"
        }

class FakeHospitalsClient:
    def __init__(self): pass
    def which_hospital(self, which_day: str = "σήμερα") -> str:
        return "🏥 ΠΓΝΠ Ρίο εφημερεύει σήμερα."

@pytest.fixture(autouse=True)
def patch_clients(monkeypatch):
    # Πείραξε τα σύμβολα ΜΕΣΑ στο module tools (εκεί τα κοιτάνε τα function_tools)
    monkeypatch.setattr(tools_mod, "PharmacyClient", FakePharmacyClient, raising=True)
    monkeypatch.setattr(tools_mod, "TimologioClient", FakeTimologioClient, raising=True)
    monkeypatch.setattr(tools_mod, "HospitalsClient", FakeHospitalsClient, raising=True)
    yield

@pytest.fixture
def client():
    return TestClient(main_mod.app)

@pytest.fixture
def clear_state():
    # helper για καθαρισμό sticky state ανά test
    def _do(sid: str):
        try:
            main_mod._clear_state(sid)
        except Exception:
            pass
    return _do
