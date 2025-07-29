# tests/test_routes.py

import os
# ΟΡΙΣΜΟΣ env vars _πριν_ το import του main
os.environ["OPENAI_API_KEY"]     = "test-key"
os.environ["PHARMACY_API_URL"]   = "http://example/pharmacy"
os.environ["PATRAS_INFO_API_URL"]   = "http://example/patras_info"
os.environ["HOSPITAL_API_URL"]   = "http://example/hospital"
os.environ["TIMOLOGIO_API_URL"]  = "http://example/timologio"
os.environ["CORS_ORIGINS"]       = "*"

import sys
import os as _os
sys.path.append(_os.path.dirname(_os.path.dirname(__file__)))
from main import create_app  # τώρα το Settings() βρίσκει όλα τα πεδία
from fastapi.testclient import TestClient
import pytest
import requests_mock

@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "OK"


def test_chat_default(client, requests_mock):
    # Mock the timologio fallback
    requests_mock.post(
        "http://example/timologio",
        json={"fare": 12.34},
    )
    resp = client.post("/chat", json={"message": "γεια"})
    assert resp.status_code == 200
    data = resp.json()
    assert "response" in data