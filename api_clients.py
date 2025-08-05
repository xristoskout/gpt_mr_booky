from typing import Dict, Any
import requests
from config import Settings
import logging

logger = logging.getLogger(__name__)


class BaseClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    def _get(self, params: Dict[str, Any] = None, path: str = "") -> Any:
        url = self.base_url + path
        try:
            response = self._session.get(url, params=params or {}, timeout=5)
            response.raise_for_status()
            return response.json()
        except ValueError:
            logger.error(f"GET {url}: Response was not JSON")
            return {"error": "API returned invalid JSON"}
        except Exception as e:
            logger.error(f"GET {url} failed: {e}")
            return {"error": str(e)}

    def _post(self, payload: Dict[str, Any] = None, path: str = "") -> Any:
        url = self.base_url + path
        try:
            response = self._session.post(url, json=payload or {}, timeout=5)
            response.raise_for_status()
            return response.json()
        except ValueError:
            logger.error(f"POST {url}: Response was not JSON")
            return {"error": "API returned invalid JSON"}
        except Exception as e:
            logger.error(f"POST {url} failed: {e}")
            return {"error": str(e)}


class PatrasInfoClient(BaseClient):
    def get_info(self, question: str = ""):
        # Το question εδώ αγνοείται, το API απλά διαβάζει αρχείο!
        data = self._post({}, path="/")
        try:
            return (
                data.get("fulfillment_response", {})
                    .get("messages", [{}])[0]
                    .get("text", {})
                    .get("text", ["Δεν βρέθηκαν πληροφορίες για Πάτρα."])[0]
            )
        except Exception:
            return "Δεν βρέθηκαν πληροφορίες για Πάτρα."

class PharmacyClient(BaseClient):
    def get_on_duty(self, area: str = "Πάτρα", method: str = "get"):
        if method == "get":
            response = self._get({"area": area}, path="/pharmacy")
            print("API RESPONSE:", response)  # Εδώ κάνεις debug τι παίρνεις από το API!
            return response
        elif method == "post":
            response = self._post({"area": area}, path="/pharmacy")
            print("API RESPONSE:", response)
            return response
        else:
            raise ValueError("Invalid method. Use 'get' or 'post'.")


class HospitalClient(BaseClient):
    def info(self, day: str = "today"):
        # Περνάει το "which_day" param στο body
        payload = {
            "queryResult": {
                "parameters": {
                    "which_day": day
                }
            }
        }
        data = self._post(payload, path="/webhook")
        # Dialogflow CX/ES payload handling:
        try:
            # Τυπικά απαντάει fulfillment_response/messages
            return (
                data.get("fulfillment_response", {})
                    .get("messages", [{}])[0]
                    .get("text", {})
                    .get("text", ["Σφάλμα στο σύστημα νοσοκομείων"])[0]
            )
        except Exception:
            return "Σφάλμα στο σύστημα νοσοκομείων"


class TimologioClient(BaseClient):
    def calculate(self, payload: Dict[str, Any]):
        # Προσθέτουμε path="/webhook"
        return self._post(payload, path="/webhook")


class PatrasLlmAnswersClient(BaseClient):
    """
    Client for the Patras LLM Answers service.

    Αν το service απαιτεί διαφορετικό endpoint ή payload, τροποποίησε
    ανάλογα τη μέθοδο answer.
    """
    def answer(self, question: str) -> Any:
        payload = {"question": question}
        return self._post(payload, path="/")


def build_clients(settings: Settings) -> Dict[str, BaseClient]:
    """
    Επιστρέφει όλα τα clients έτοιμα για injection.
    Προσοχή: Όλα τα URLs στα settings **χωρίς path στο τέλος!**
    """
    return {
        "pharmacy": PharmacyClient(settings.pharmacy_api_url),
        "hospital": HospitalClient(settings.hospital_api_url),
        "timologio": TimologioClient(settings.timologio_api_url),
        # Το κλειδί μπορεί να παραμείνει με παύλες για συμβατότητα,
        # η κλάση όμως πρέπει να έχει έγκυρο όνομα.
        "patras-llm-answers": PatrasLlmAnswersClient(settings.patras_llm_answers_api_url),
    }
