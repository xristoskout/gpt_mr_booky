# integrations/infoxoros_api.py
from __future__ import annotations
import os
from typing import Optional, Dict, Any
import requests

# ──────────────────────────────────────────────────────────────────────────────
# Παραμετρικά endpoints / headers από .env (ασφαλές για παραγωγή)
BASE = os.getenv("INFOXOROS_BASE_URL", "https://apps.taxifast.gr/call-api/info/")
CREATE_URL = os.getenv("INFOXOROS_CREATE_URL", BASE)              # αν διαφέρει στο submit
CREATE_ACTION = os.getenv("INFOXOROS_CREATE_ACTION", "create")    # π.χ. "create" ή κάτι άλλο

REQUIRE_ORIGIN = os.getenv("INFOXOROS_REQUIRE_ORIGIN", "0") == "1"
ORIGIN = os.getenv("INFOXOROS_ORIGIN", "https://booking.infoxoros.com")
REFERER = os.getenv("INFOXOROS_REFERER", "https://booking.infoxoros.com/")

API_KEY = os.getenv("INFOXOROS_API_KEY", "")
URL_KEY = os.getenv("INFOXOROS_URL_KEY", "")  # π.χ. cbe08ae5-...
AGENT = os.getenv("INFOXOROS_AGENT", "patraexpress")
AGENT_EMAIL = os.getenv("INFOXOROS_AGENT_EMAIL", "radiotaxipatras@gmail.com")
DEFAULT_PAYWAY = os.getenv("INFOXOROS_PAYWAY", "Μετρητά στον οδηγό")
MAIL_TITLE = os.getenv("INFOXOROS_MAIL_TITLE", "Patra Express")
SEND_MAIL = os.getenv("INFOXOROS_SEND_MAIL", "true")              # "true"/"false"
WITH_AUTOTAXI = os.getenv("INFOXOROS_WITH_AUTOTAXI", "1")         # "1"/"0"
TIMEOUT = float(os.getenv("INFOXOROS_TIMEOUT_SEC", "12"))
COOKIE = os.getenv("INFOXOROS_COOKIE", "")

def _headers() -> Dict[str, str]:
    h = {}
    if REQUIRE_ORIGIN:
        h.update({
            "Origin": ORIGIN,
            "Referer": REFERER,
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
    if COOKIE:
        h["Cookie"] = COOKIE
    return h


def _post(url: str, data: dict, timeout: float = TIMEOUT) -> dict:
    r = requests.post(url, data=data, headers=_headers(), timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"raw": r.text, "status_http": r.status_code}

# ──────────────────────────────────────────────────────────────────────────────
# Public helpers
def build_booking_link(*, lang: str = "el") -> str:
    """
    Επιστρέφει το deep-link της φόρμας με το URL key και γλώσσα.
    """
    key = URL_KEY or ""
    base = "https://booking.infoxoros.com/"
    if not key:
        return f"{base}?lang={lang}"
    return f"{base}?key={key}&lang={lang}"

# ──────────────────────────────────────────────────────────────────────────────
# Cost calculator (frontend API – χρήσιμο για UX/estimate)
def cost_calculator(
    *, lat_start: float, lon_start: float, lat_end: float, lon_end: float, time_hhmm: Optional[str] = None
) -> dict:
    params = {
        "latStart": lat_start, "lonStart": lon_start,
        "latEnd": lat_end, "lonEnd": lon_end,
        "agent": AGENT,
    }
    if time_hhmm:
        params["randevou"] = 1
        params["time"] = time_hhmm

    r = requests.get("https://booking.infoxoros.com/api/cost_calculator.php", params=params, timeout=8)
    r.raise_for_status()
    j = r.json()

    # Εμπλουτισμός με εύχρηστες τιμές
    out = dict(j)
    try:
        cost_str = str(j.get("cost", "")).replace(",", ".")
        out["cost_float"] = float(cost_str) if cost_str else None
    except Exception:
        out["cost_float"] = None
    try:
        m = float(j.get("distance", 0))
        out["distance_km"] = round(m / 1000.0, 2)
    except Exception:
        out["distance_km"] = None
    try:
        sec = float(j.get("duration", 0))
        out["duration_min"] = int(round(sec / 60.0))
    except Exception:
        out["duration_min"] = None
    return out

# ──────────────────────────────────────────────────────────────────────────────
# Προέγκριση/estimate από backend (δεν δημιουργεί κράτηση)
def get_offer(point1: str, point2: str, when_iso: str, phone: str = "") -> dict:
    """
    action=getoffer στο BASE (/call-api/info/).
    point1/point2: "lat,lon"
    when_iso: "YYYY-MM-DD HH:MM:SS" (τοπική ώρα Ελλάδας)
    """
    if not API_KEY:
        return {"status": 0, "errors": ["missing api key"]}
    data = {
        "apikey": API_KEY,
        "action": "getoffer",
        "point1": point1,
        "point2": point2,
        "datetime": when_iso,
        "phone": phone or "",
    }
    return _post(BASE, data)

# ──────────────────────────────────────────────────────────────────────────────
# Δημιουργία κράτησης (αν επιτρέπεται από backend). Αν όχι, ο caller κάνει fallback.
def _fmt_distance_km(km: Optional[float]) -> str:
    return f"{km:.2f} χλμ." if isinstance(km, (int, float)) else ""

def _fmt_duration_mmhh(m: Optional[int]) -> str:
    if not isinstance(m, int):
        return ""
    h = m // 60
    mm = m % 60
    return f"{h:02d}:{mm:02d}"

def create_booking(
    *,
    origin_address: str,
    destination_address: str,
    point1: str,  # "lat,lon"
    point2: str,  # "lat,lon"
    when_iso: str,  # "YYYY-MM-DD HH:MM:SS" (τοπική GR)
    name: str,
    phone: str,
    email: str = "",
    pax: int = 1,
    luggage_count: int = 0,
    remarks: str = "",
    pay_way: str = DEFAULT_PAYWAY,
    lang: str = "el",
    url_key: Optional[str] = None,
    # Optional / visual fields όπως στέλνει η φόρμα
    use_estimate: bool = True,
    captcha: Optional[str] = None,
    specials: str = "0,0,0,0,0",
    specials_text: str = "",
    num_cars: int = 1,
    ship: str = "",
    with_autotaxi: Optional[str] = None,
    extra_email_text: str = "",
    agent: Optional[str] = None,
    agent_email: Optional[str] = None,
) -> dict:
    """
    Υποβολή με action παραμετρικό από .env (CREATE_ACTION) στο CREATE_URL.
    Αν ο server δεν δέχεται create χωρίς captcha, θα επιστρέψει σφάλμα — ο caller πρέπει να κάνει fallback.
    """
    if not API_KEY:
        return {"status": 0, "errors": ["missing api key"]}

    # Προαιρετικά: estimate για να γεμίσουμε distance/duration/cost
    distance_s = duration_s = cost_s = all_cost_s = ""
    if use_estimate:
        try:
            lat1, lon1 = map(float, point1.split(","))
            lat2, lon2 = map(float, point2.split(","))
            est = cost_calculator(lat_start=lat1, lon_start=lon1, lat_end=lat2, lon_end=lon2)
            distance_s = _fmt_distance_km(est.get("distance_km"))
            duration_s = _fmt_duration_mmhh(est.get("duration_min"))
            if est.get("cost_float") is not None:
                cost_s = f"{est['cost_float']:.2f} €"
                all_cost_s = cost_s
        except Exception:
            pass

    data = {
        "lang": lang,
        "url_key": url_key or URL_KEY,
        "apikey": API_KEY,
        "payWay": pay_way,
        # ΜΟΝΟ αν έχει οριστεί· στο appointment.php συνήθως δεν χρειάζεται action
        # (αν το βάλεις κενό, κάποιοι servers γυρίζουν "invalid action")
        # Άρα:
        #   if CREATE_ACTION: data["action"] = CREATE_ACTION             # ← παραμετρικό
        "appdate": when_iso,
        "name": name,
        "phone": phone,
        "address1": origin_address,
        "address2": destination_address,
        "point1": point1,
        "point2": point2,
        "specials": specials,
        "remarks": remarks or "—",
        "email": email or "",
        "numofcars": str(num_cars),
        "people_count": str(pax),
        "luggage_count": str(luggage_count),
        "ship": ship,
        "distance": distance_s,
        "duration": duration_s,
        "cost": cost_s,
        "all_cost": all_cost_s or cost_s,
        "specials_text": specials_text,
        "mailTitle": MAIL_TITLE,
        "sendMail": SEND_MAIL,
        "agent": agent or AGENT,
        "agentEmail": agent_email or AGENT_EMAIL,
        "with_autotaxi": WITH_AUTOTAXI if with_autotaxi is None else with_autotaxi,
        "extra_email_text": extra_email_text,
    }
    if CREATE_ACTION:
        data["action"] = CREATE_ACTION
    return _post(CREATE_URL, data)

# ──────────────────────────────────────────────────────────────────────────────
__all__ = [
    "build_booking_link",
    "cost_calculator",
    "get_offer",
    "create_booking",
]
