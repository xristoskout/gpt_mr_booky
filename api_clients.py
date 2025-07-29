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

def build_clients(settings: Settings):
    return {
        "pharmacy": BaseClient(settings.pharmacy_api_url),
        "hospital": BaseClient(settings.hospital_api_url),
        "timologio": BaseClient(settings.timologio_api_url),
        "patras": PatrasInfoClient(settings.patras_info_api_url),
    }
