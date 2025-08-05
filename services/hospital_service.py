import requests

class HospitalClient:
    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    def info(self) -> str:
        try:
            resp = self._session.post(
                self.base_url, json={}, timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("fulfillmentText", "Δεν βρέθηκαν πληροφορίες νοσοκομείων.")
        except requests.RequestException as e:
            # logger.error(f"Hospital API error: {e}")
            return "Το σύστημα εφημερευόντων νοσοκομείων δεν είναι διαθέσιμο αυτή τη στιγμή."
