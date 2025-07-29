import requests
from typing import Any, Dict


class TimologioClient:
    """
    Client για το Timologio API.
    Παρέχει μέθοδο calculate(...) που δέχεται το payload και
    επιστρέφει το JSON ή {"error": "..."} σε περίπτωση αποτυχίας.
    """

    def __init__(self, base_url: str, timeout: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    def calculate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            resp = self._session.post(
                f"{self.base_url}/calculate_fare",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            return {"error": "Το σύστημα τιμολόγησης δεν είναι διαθέσιμο αυτή τη στιγμή."}
