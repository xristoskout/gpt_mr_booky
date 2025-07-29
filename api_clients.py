from typing import Dict, Any
import requests
from config import Settings
import logging

logger = logging.getLogger(__name__)

class BaseClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _get(self, params: Dict[str, Any] = None):
        try:
            response = requests.get(self.base_url, params=params or {}, timeout=5)
            return response.json()
        except Exception as e:
            logger.error(f"GET failed: {e}")
            return {"error": str(e)}

    def _post(self, payload: Dict[str, Any] = None):
        try:
            response = requests.post(self.base_url, json=payload or {}, timeout=5)
            return response.json()
        except Exception as e:
            logger.error(f"POST failed: {e}")
            return {"error": str(e)}

class PatrasInfoClient(BaseClient):
    def get_info(self, question: str):
        return self._post({"query": question})


class PharmacyClient(BaseClient):
    """Client wrapper for pharmacy API"""

    def get_on_duty(self, area: str = "Πάτρα"):
        return self._get({"area": area})


class HospitalClient(BaseClient):
    """Client wrapper for hospital API"""

    def info(self):
        data = self._post({})
        return data.get("fulfillmentText", "Σφάλμα στο σύστημα νοσοκομείων")


class TimologioClient(BaseClient):
    """Client wrapper for trip cost API"""

    def calculate(self, payload: Dict[str, Any]):
        return self._post(payload)


class DistanceClient(BaseClient):
    """Client wrapper for distance API"""

    def route_and_fare(self, origin: str, destination: str):
        return self._post({"origin": origin, "destination": destination})

def build_clients(settings: Settings):
    return {
        "pharmacy": PharmacyClient(settings.pharmacy_api_url),
        "hospital": HospitalClient(settings.hospital_api_url),
        "timologio": TimologioClient(settings.timologio_api_url),
        "patras": PatrasInfoClient(settings.patras_info_api_url),
    }