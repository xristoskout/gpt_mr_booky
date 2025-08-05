# services/patras.py
import requests

class PatrasInfoClient:
    def __init__(self, base_url: str):
        self.url = base_url

    def get_info(self) -> str:
        try:
            res = requests.get(self.url, timeout=5)
            res.raise_for_status()
            data = res.json()
            entries = data.get("entries", [])
            if not entries:
                return "Δεν βρήκα χρήσιμες πληροφορίες αυτή τη στιγμή."
            return "\n".join(f"• {e['title']}: {e['phone']}" for e in entries)
        except Exception as e:
            return f"⚠️ Σφάλμα κατά την ανάκτηση δεδομένων: {e}"
