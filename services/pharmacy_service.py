# services/pharmacy_service.py

import requests
from typing import Dict, Any


class PharmacyClient:
    """
    Client για το Pharmacy API.
    Παρέχει μέθοδο get_on_duty(area) που επιστρέφει το JSON από το API
    ή ένα {"error": ...} σε περίπτωση σφάλματος.
    """

    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    def get_on_duty(self, area: str = "Πάτρα"):
        try:
            resp = self._session.get(
                f"{self.base_url}/pharmacy",
                params={"area": area},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Pharmacy API area={area} → {data}")
            return data
        except requests.RequestException as e:
            logger.error(f"Pharmacy API error: {e}")
            return {
                "error": "Το σύστημα εφημερευόντων φαρμακείων δεν είναι διαθέσιμο αυτή τη στιγμή."
            }
