# tests/test_routes.py

import os
# ΟΡΙΣΜΟΣ env vars _πριν_ το import του main
os.environ["OPENAI_API_KEY"]     = "test-key"
os.environ["PHARMACY_API_URL"]   = "http://example/pharmacy"
os.environ["DISTANCE_API_URL"]   = "http://example/distance"
os.environ["HOSPITAL_API_URL"]   = "http://example/hospital"
os.environ["TIMOLOGIO_API_URL"]  = "http://example/timologio"
os.environ["CORS_ORIGINS"]       = "*"

from main import create_app  # τώρα το Settings() βρίσκει όλα τα πεδία
import pytest
import requests_mock

@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.data == b"OK"


def test_chat_default(client, requests_mock):
    # Mock the timologio fallback
    requests_mock.post(
        "http://example/timologio",
        json={"fare": 12.34},
    )
    resp = client.post("/chat", json={"message": "γεια"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "reply" in data
