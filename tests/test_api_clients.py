# tests/test_api_clients.py

import pytest
import requests
from api_clients import (
    PharmacyClient,
    HospitalClient,
    TimologioClient,
)


def test_pharmacy_success(requests_mock):
    url = "http://api/pharmacy"
    client = PharmacyClient(url)
    requests_mock.get(url, json={"pharmacies": [{"name":"A","address":"B","time_range":"C"}]})
    data = client.get_on_duty("Πάτρα")
    assert data["pharmacies"][0]["name"] == "A"


def test_pharmacy_error(requests_mock):
    url = "http://api/pharmacy"
    client = PharmacyClient(url)
    requests_mock.get(url, exc=requests.exceptions.ConnectTimeout)
    data = client.get_on_duty("Πάτρα")
    assert "error" in data


def test_hospital_success(requests_mock):
    url = "http://api/hospital"
    client = HospitalClient(url)
    requests_mock.post(url, json={"fulfillmentText": "OK"})
    assert client.info() == "OK"


def test_hospital_error(requests_mock):
    url = "http://api/hospital"
    client = HospitalClient(url)
    requests_mock.post(url, exc=requests.exceptions.ReadTimeout)
    text = client.info()
    assert isinstance(text, str) and text.startswith("Σφάλμα")


def test_timologio_success(requests_mock):
    url = "http://api/timologio"
    client = TimologioClient(url)
    payload = {"distance_km": 5.0}
    requests_mock.post(url, json={"total_fare": 42.0})
    data = client.calculate(payload)
    assert data["total_fare"] == 42.0


def test_timologio_error(requests_mock):
    url = "http://api/timologio"
    client = TimologioClient(url)
    requests_mock.post(url, exc=requests.exceptions.ConnectTimeout)
    data = client.calculate({})
    assert "error" in data
