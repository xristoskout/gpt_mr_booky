import requests


class HospitalClient:
    """
    Client για το Hospital Webhook (ExpressJS).
    Παρέχει μέθοδο info() που επιστρέφει το fulfillmentText
    ή μήνυμα σφάλματος.
    """

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
        except requests.RequestException:
            return "Το σύστημα εφημερευόντων νοσοκομείων δεν είναι διαθέσιμο αυτή τη στιγμή."
