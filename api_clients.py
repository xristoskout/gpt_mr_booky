import os
import logging
import re
from typing import Any, Dict, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def _env_url(*names: str) -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v.rstrip("/")
    return ""


class BaseClient:
    def __init__(self, base_url_env: str, default_path: str = "/", timeout: int = 10, *, alt_env: tuple[str, ...] = ()): 
        base = _env_url(base_url_env, *alt_env)
        if not base:
            raise RuntimeError(f"Missing base URL for {base_url_env} in .env")
        self.base_url = base
        self.default_path = default_path
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json"})
        token = os.getenv("SERVICE_BEARER_TOKEN")  # προαιρετικό
        if token:
            self.s.headers.update({"Authorization": f"Bearer {token}"})

        retries = Retry(total=3, backoff_factor=0.3, status_forcelist=[502, 503, 504])
        self.s.mount("https://", HTTPAdapter(max_retries=retries))
        self.s.mount("http://", HTTPAdapter(max_retries=retries))

    def _url(self, path: Optional[str] = None) -> str:
        p = (path or self.default_path).lstrip("/")
        return f"{self.base_url}/{p}"

    def _parse(self, resp: requests.Response) -> Any:
        if not resp.content:
            return None
        ctype = resp.headers.get("Content-Type", "")
        # Cloud Run μερικές φορές επιστρέφει text/html με JSON μέσα· δοκίμασε πρώτα JSON
        try:
            return resp.json()
        except Exception:
            pass
        try:
            txt = resp.text or ""
            # μικρό safety: αν μοιάζει με JSON, προσπάθησε δεύτερη φορά
            if txt.strip().startswith("{") or txt.strip().startswith("["):
                import json
                return json.loads(txt)
            return txt
        except Exception:
            return None

    def _get(self, params: Dict[str, Any], path: Optional[str] = None) -> Any:
        resp = self.s.get(self._url(path), params=params, timeout=self.timeout)
        resp.raise_for_status()
        return self._parse(resp)

    def _post(self, data: Dict[str, Any], path: Optional[str] = None) -> Any:
        resp = self.s.post(self._url(path), json=data, timeout=self.timeout)
        resp.raise_for_status()
        return self._parse(resp)


# ===================== PHARMACY =====================

class PharmacyClient(BaseClient):
    def __init__(self):
        # Πλέον default_path = "pharmacy" (όχι "on_duty")
        super().__init__("PHARMACY_API_URL", default_path="pharmacy", alt_env=("PHARMACY_API_BASE",))

    def get_on_duty(self, area: str = "Πάτρα", method: str = "get") -> Dict[str, Any]:
        """
        Καλεί ΜΟΝΟ /pharmacy (GET ή POST) και επιστρέφει ενιαίο dict:
        { "area": "<περιοχή>", "pharmacies": [ {name, address, time_range}, ... ] }
        """
        area = (area or "Πάτρα").strip()
        try:
            if method.lower() == "post":
                data = self._post({"area": area}, path="pharmacy")
            else:
                data = self._get({"area": area}, path="pharmacy")

            # Ομογενοποίηση απάντησης
            if isinstance(data, list):
                pharmacies = data
            elif isinstance(data, dict):
                pharmacies = data.get("pharmacies", [])
            else:
                pharmacies = []

            return {"area": area, "pharmacies": pharmacies}

        except Exception:
            logger.exception("PharmacyClient.get_on_duty failed")
            return {"area": area, "pharmacies": []}

# ===================== HOSPITALS =====================

class HospitalsClient(BaseClient):
    def __init__(self):
        super().__init__("HOSPITAL_API_URL", default_path="webhook", alt_env=("HOSPITAL_API_BASE",))

    def which_hospital(self, which_day: str = "σήμερα") -> str:
        wd = (which_day or "").strip().lower()
        is_today = wd in ("σήμερα", "σημερα", "today")
        is_tomorrow = wd in ("αύριο", "αυριο", "tomorrow")

        def _call(payload):
            try:
                data = self._post(payload, path="webhook")
            except Exception:
                logger.exception("Hospitals webhook call failed")
                return None

            # 1) {"reply": "..."}
            if isinstance(data, dict) and isinstance(data.get("reply"), str):
                return data["reply"]

            # 2) Dialogflow-like
            try:
                msgs = data.get("fulfillment_response", {}).get("messages", [])
                if msgs and "text" in msgs[0]:
                    texts = msgs[0]["text"].get("text", [])
                    if texts:
                        return texts[0]
            except Exception:
                pass

            # 3) Raw
            return data if isinstance(data, str) else None

        # 1η προσπάθεια
        ans = _call({"queryResult": {"parameters": {"which_day": which_day}}})
        if ans and (is_today or not is_tomorrow):
            return ans

        # Αν ζητήθηκε αύριο, κάνε αγγλικό fallback
        if is_tomorrow:
            for payload in (
                {"queryResult": {"parameters": {"which_day": "tomorrow"}}},
                {"queryResult": {"parameters": {"day": "tomorrow"}}},
            ):
                ans2 = _call(payload)
                if ans2:
                    return ans2

        return "❌ Δεν μπόρεσα να ανακτήσω την εφημερία νοσοκομείων."


# ===================== PATRAS LLM ANSWERS =====================

class PatrasAnswersClient(BaseClient):
    def __init__(self):
        super().__init__(
            "PATRAS_LLM_ANSWERS_API_URL",
            default_path="",
            alt_env=("PATRAS_LLM_ANSWERS_API_BASE", "PATRAS_ANSWERS_API_BASE"),
        )

    def ask(self, message: str, user_id: str = "agent_router") -> str:
        payload = {"question": message}
        try:
            data = self._post(payload, path="")
        except Exception:
            logger.exception("PatrasAnswers API call failed")
            return "❌ Δεν μπόρεσα να ανακτήσω πληροφορίες για την Πάτρα."

        if isinstance(data, dict):
            if isinstance(data.get("answer"), str):
                return data["answer"]
            if isinstance(data.get("reply"), str):
                return data["reply"]
        return str(data)


# ===================== TIMOLOGIO =====================

class TimologioClient(BaseClient):
    """
    Υπολογιστής κόστους διαδρομών ταξί.
    Προσπαθεί πρώτα GET /fare, μετά POST /webhook (Dialogflow-like).
    Επιστρέφει dict με κλειδιά: price_eur, distance_km, duration_min, map_url (όπου γίνεται).
    """
    def __init__(self):
        super().__init__("TIMOLOGIO_API_URL", default_path="fare", alt_env=("TIMOLOGIO_API_BASE",))

    def estimate_trip(self, origin: str, destination: str, when: str = "now") -> Dict[str, Any]:
        # 1) GET /fare (δύο παραλλαγές)
        get_variants = [
            {"origin": origin, "destination": destination, "when": when},
            {"from": origin, "to": destination, "when": when},
        ]
        last = None
        for params in get_variants:
            try:
                data = self._get(params, path="fare")
                last = data

                # standardize: αν έχει 'fare' αλλά όχι 'price_eur', αντέγραψέ το
                if isinstance(data, dict) and "fare" in data and "price_eur" not in data:
                    data["price_eur"] = data["fare"]

                # αν έχουμε ήδη τιμή, γύρνα το όπως είναι
                has_price = isinstance(data, dict) and any(
                    k in data for k in ("price_eur", "price", "total_eur", "fare", "amount", "total")
                )
                if has_price:
                    return data

                # αλλιώς πήγαινε για enrichment με POST /webhook
                enriched = self._enrich_via_webhook(origin, destination, when)
                if isinstance(enriched, dict):
                    merged = dict(data)
                    merged.update({k: v for k, v in enriched.items() if v is not None})
                    # ξανά standardize
                    if "fare" in merged and "price_eur" not in merged:
                        merged["price_eur"] = merged["fare"]
                    return merged

                return data
            except Exception:
                logger.warning("Timologio GET /fare failed with params=%s", params, exc_info=True)

        # 2) Αν απέτυχε το GET, δοκίμασε κατευθείαν POST
        enriched = self._enrich_via_webhook(origin, destination, when)
        if isinstance(enriched, dict):
            # ξανά standardize
            if "fare" in enriched and "price_eur" not in enriched:
                enriched["price_eur"] = enriched["fare"]
            return enriched

        return {"error": "unavailable"}

    # -------------- helpers --------------

    def _enrich_via_webhook(self, origin: str, destination: str, when: str) -> Dict[str, Any]:
        post_variants = [
            {"origin": origin, "destination": destination, "when": when},
            {"from": origin, "to": destination, "when": when},
        ]
        for body in post_variants:
            try:
                df = self._post(body, path="webhook")
                # Dialogflow-like
                if isinstance(df, dict) and "fulfillment_response" in df:
                    parsed = self._parse_timologio(df)
                    # κράτα και top-level fields αν υπαρχουν (map_url κ.λπ.)
                    if df.get("map_url") and not parsed.get("map_url"):
                        parsed["map_url"] = df["map_url"]
                    if df.get("route_url") and not parsed.get("map_url"):
                        parsed["map_url"] = df["route_url"]
                    return parsed
                # ωμός dict
                if isinstance(df, dict):
                    return df
            except Exception:
                logger.warning("Timologio POST /webhook enrichment failed", exc_info=True)
        return {}

    @staticmethod
    def _parse_timologio(df_like: dict) -> dict:
        """
        Parse Dialogflow-like webhook reply → {price_eur, distance_km, duration_min, map_url}.
        Υποστηρίζει:
        - Τιμή με σύμβολο € (123,45 €)
        - Απόσταση σε χλμ
        - Διάρκεια "X ώρες και Y λεπτά", "HH:MM" ή "~NN λεπτά"
        - map_url είτε σε ξεχωριστό πεδίο είτε μέσα σε <a href="...">
        """
        res: Dict[str, Any] = {}

        try:
            txt = (
                df_like.get("fulfillment_response", {})
                .get("messages", [])[0]
                .get("text", {})
                .get("text", [])[0]
            ) or ""

            # --- map url: από explicit key ή από anchor href ---
            map_url = df_like.get("map_url") or df_like.get("route_url")
            if not map_url:
                m_href = re.search(
                    r"""href=['"](?P<href>https?://www\.google\.com/maps/dir/\?[^'"]+)['"]""",
                    txt, flags=re.IGNORECASE
                )
                if m_href:
                    map_url = m_href.group("href")
            if map_url:
                res["map_url"] = map_url

            # --- price ---
            m = re.search(r"([\d.,]+)\s*€", txt)
            if m:
                try:
                    res["price_eur"] = float(m.group(1).replace(".", "").replace(",", "."))
                except Exception:
                    pass

            # --- distance ---
            m = re.search(r"([\d.,]+)\s*χλμ", txt, flags=re.IGNORECASE)
            if m:
                try:
                    res["distance_km"] = float(m.group(1).replace(".", "").replace(",", "."))
                except Exception:
                    pass

            # --- duration ---
            dur_min = None

            # "X ώρες και Y λεπτά"
            m = re.search(r"~?\s*(\d+)\s*ώρ(?:ες|α)?\s*και\s*(\d+)\s*λεπ", txt, flags=re.IGNORECASE)
            if m:
                dur_min = int(m.group(1)) * 60 + int(m.group(2))

            # HH:MM (π.χ. 2:24 ή 113:03)
            if dur_min is None:
                m = re.search(r"\b(\d{1,3})[:.](\d{2})\b", txt)
                if m:
                    dur_min = int(m.group(1)) * 60 + int(m.group(2))

            # "~NN λεπτά"
            if dur_min is None:
                m = re.search(r"~?\s*(\d+)\s*λεπ", txt, flags=re.IGNORECASE)
                if m:
                    dur_min = int(m.group(1))

            if dur_min is not None:
                res["duration_min"] = dur_min

        except Exception:
            logger.exception("Timologio _parse_timologio failed")

        return res
