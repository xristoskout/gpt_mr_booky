# tests/test_response_formatters.py

import pytest
from response_formatters import format_pharmacies

def test_format_pharmacies_empty():
    out = format_pharmacies({"pharmacies": []})
    assert out == "Δεν βρέθηκαν εφημερεύοντα φαρμακεία."

def test_format_pharmacies_error():
    err = format_pharmacies({"error": "oops"})
    assert err == "oops"

def test_format_pharmacies_normal():
    data = {
        "pharmacies": [
            {"name": "Φαρμακείο A", "address": "Διεύθυνση B", "time_range": "09:00-17:00"}
        ]
    }
    out = format_pharmacies(data)
    assert "- Φαρμακείο A, Διεύθυνση B (09:00-17:00)" in out
