# tools.py
import json
import math
import os
import logging
from typing import Any, Dict, List, Optional, Tuple
import re

import unicodedata
from urllib.parse import quote_plus
import math
from agents import function_tool, RunContextWrapper  # OpenAI Agents SDK
import openai
from constants import TAXI_TARIFF
import constants

# Ασφαλή “getattr” ώστε να μην σκάει το import αν λείπει κάτι στο constants.py
BRAND_INFO = getattr(constants, "BRAND_INFO", {})
DEFAULTS   = getattr(constants, "DEFAULTS", {})
UI_TEXT    = getattr(constants, "UI_TEXT", {})
AREA_ALIASES = getattr(constants, "AREA_ALIASES", {})


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ──────────────────────────────────────────────────────────────────────────────
# Βοηθητικά για κανονικοποίηση κειμένου (για περιοχές)
def _deaccent(s: str) -> str:
    if not s:
        return ""
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")

def _norm_txt(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = _deaccent(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

# ──────────────────────────────────────────────────────────────────────────────
# Brand info (fallback: .env)
def _brand(key: str, env_fallback: Optional[str] = None) -> str:
    val = BRAND_INFO.get(key)
    if val:
        return val
    if env_fallback:
        return os.getenv(env_fallback, "")
    return ""

# ── Clients (graceful fallback) ───────────────────────────────────────────────
try:
    from api_clients import (
        PharmacyClient,
        HospitalsClient,
        PatrasAnswersClient,
        TimologioClient,
    )
except Exception:
    PharmacyClient = HospitalsClient = PatrasAnswersClient = TimologioClient = None

# ──────────────────────────────────────────────────────────────────────────────
# LLM helper με system prompt από context
def _ask_llm_with_system_prompt(
    user_message: str,
    system_prompt: str,
    context_text: str = "",
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    openai.api_key = os.getenv("OPENAI_API_KEY")
    from openai import OpenAI
    client = OpenAI()

    history_msgs = []
    if history:
        for h in history[-2:]:
            if h.get("user"):
                history_msgs.append({"role": "user", "content": h["user"]})
            if h.get("bot"):
                history_msgs.append({"role": "assistant", "content": h["bot"]})

    messages = [{"role": "system", "content": system_prompt}] + history_msgs + [
        {"role": "user", "content": f"{user_message}\n\n[Context]\n{context_text}"}
    ]

    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        temperature=0.7,
        presence_penalty=0.6,
        frequency_penalty=0.2,
    )
    return resp.choices[0].message.content


@function_tool(
    name_override="ask_llm",
    description_override="Στέλνει μήνυμα στο LLM με system prompt από το context, μαζί με optional context_text & history.",
)
def ask_llm(ctx: RunContextWrapper[Any], user_message: str) -> str:
    try:
        c = ctx.context or {}

        # Αν υπάρχει explicit desired_tool και ΔΕΝ είναι το ask_llm, μην απαντήσεις από εδώ.
        desired = c.get("desired_tool")
        if desired and desired != "ask_llm":
            return "⏭️"

        # --- εδώ κάνεις την πραγματική κλήση στο LLM σου ---
        system_prompt = c.get("system_prompt") or "You are a helpful assistant."
        context_text  = c.get("context_text") or ""
        history       = c.get("history") or []

        # Παράδειγμα: llm_chat είναι δικό σου helper
        reply = llm_chat(
            system=system_prompt,
            user=user_message,
            context=context_text,
            history=history,
        )
        return reply

    except Exception:
        logger.exception("ask_llm failed")
        return UI_TEXT.get("generic_error", "❌ Παρουσιάστηκε σφάλμα κατά την κλήση του LLM.")

# ──────────────────────────────────────────────────────────────────────────────
# Fallback εκτίμηση (όταν το Timologio API δεν απαντά)
def _rough_distance_km(origin: str, destination: str) -> float:
    known = {
        ("πάτρα", "αθήνα"): 275.0,
        ("patra", "athens"): 275.0,
        ("πάτρα", "πρέβεζα"): 220.0,
        ("πάτρα", "καλαμάτα"): 210.0,
        ("πάτρα", "λουτράκι"): 184.0,
    }
    key = (origin.lower().strip(), destination.lower().strip())
    return known.get(key, 200.0)

def _estimate_price_and_time_km(distance_km: float) -> Dict[str, Any]:
    start_fee = TAXI_TARIFF.get("minimum_fare", 4.0)
    per_km    = TAXI_TARIFF.get("km_rate_zone2_or_night", 1.25)  # για intercity
    avg_kmh   = 85.0
    duration_h = distance_km / avg_kmh
    duration_min = int(round(duration_h * 60))
    cost = start_fee + per_km * distance_km
    return {
        "distance_km": round(distance_km, 1),
        "duration_min": duration_min,
        "price_eur": round(cost, 2),
    }

# ──────────────────────────────────────────────────────────────────────────────
# NLP parsing για διαδρομές
PLACE_SEP_PAT = r"(?:\s*[-–>|]\s*|\s+)"

def _extract_route_free_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Γυρίζει (origin, dest) από ελεύθερο ελληνικό κείμενο.
    Αν λείπει origin, default = 'πάτρα'.
    Πιάνει:
      - "από Πάτρα μέχρι Λουτράκι"
      - "πάτρα μέχρι λουτράκι"
      - "πάτρα-λουτράκι"
      - "μέχρι λουτράκι;" (dest only)
    """
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = re.sub(r"\s+", " ", t).strip(" ;,.;¿;;;?")

    patterns = [
        r"απ[όο]\s+(?P<origin>.+?)\s+(?:μέχρι|ως|προς|για|σε)\s+(?P<dest>.+)",
        r"από\s+(?P<origin>.+?)\s+(?:μέχρι|ως|προς|για|σε)\s+(?P<dest>.+)",
        r"^(?P<origin>[^0-9]+?)\s+(?:μέχρι|προς|για|σε)\s+(?P<dest>.+)$",
        rf"^(?P<origin>[a-zα-ωάέίόήύώ\. ]+){PLACE_SEP_PAT}(?P<dest>[a-zα-ωάέίόήύώ\. ]+)$",
        r"πόσο\s+(?:κάνει|κοστίζει)\s+(?:να\s+)?(?:πάω|πάμε|μετάβαση)\s+(?:σε|προς)?\s*(?P<dest>[a-zα-ωάέίόήύώ\. ]+)$",
        r"^(?:μέχρι|προς|για)\s+(?P<dest>[a-zα-ωάέίόήύώ\. ]+)$",
    ]

    for pat in patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            origin = (m.groupdict().get("origin") or "").strip(" ,.;")
            dest   = (m.groupdict().get("dest")   or "").strip(" ,.;")
            if not origin:
                origin = "πάτρα"
            return origin, dest if dest else None
    return None, None

def _normalize_minutes(val, distance_km=None):
    """
    Δέχεται λεπτά, δευτερόλεπτα ("1234s"), "HH:MM", "2 ώρες ...", ISO8601 τύπου PT2H30M45S
    και τα γυρίζει σε λεπτά (int).
    """
    if val is None:
        return None

    # numeric
    if isinstance(val, (int, float)):
        m = int(round(val))
        if m > 1800:  # πιθανότατα seconds
            m = int(round(m / 60))
        return m

    s = str(val).strip().lower()
    # "1234s" -> seconds
    ms = re.match(r"^(\d+)\s*s$", s)
    if ms:
        return int(ms.group(1)) // 60

    # HH:MM
    mm = re.search(r"\b(\d{1,3})[:.](\d{2})\b", s)
    if mm:
        return int(mm.group(1)) * 60 + int(mm.group(2))

    # ISO8601 PT… (π.χ. PT2H30M45S)
    if s.startswith("pt"):
        h = re.search(r"(\d+)h", s)
        m = re.search(r"(\d+)m", s)
        sec = re.search(r"(\d+)s", s)
        mins = 0
        if h: mins += int(h.group(1)) * 60
        if m: mins += int(m.group(1))
        if sec: mins += int(sec.group(1)) // 60
        if mins:
            return mins

    # ελληνικά: "2 ώρες και 15 λεπτά", "45 λεπτά"
    m1 = re.search(r"(\d+)\s*ώρ", s)
    m2 = re.search(r"(\d+)\s*λεπ", s)
    if m1 and m2:
        return int(m1.group(1)) * 60 + int(m2.group(1))
    if m2:
        return int(m2.group(1))
    if s.isdigit():
        return int(s)

    # safety net απόστασης
    if distance_km:
        approx = int(round((float(distance_km) / 85.0) * 60))
        if approx > 0:
            return approx
    return None

def _fmt_minutes(mins: Optional[int]) -> Optional[str]:
    if mins is None:
        return None
    try:
        m = int(mins)
    except Exception:
        return None
    h, r = divmod(m, 60)
    if h and r:
        return f"{h} ώρες και {r} λεπτά"
    if h:
        return f"{h} ώρες"
    return f"{r} λεπτά"

@function_tool
def trip_quote_nlp(message: str, when: str = "now") -> str:
    """
    Βγάζει origin/destination από ελεύθερο ελληνικό κείμενο και καλεί το TIMOLOGIO API.
    - Αν δοθεί μόνο προορισμός, origin = 'Πάτρα'.
    - ΠΑΝΤΑ επιστρέφει και το Google Maps URL *μέσα στο κείμενο* για να φτιαχτεί κουμπί από το frontend.
    - Η διάρκεια είναι σε μορφή "Χ ώρες και Υ λεπτά".
    """
    logger.info("[tool] trip_quote_nlp: parsing route from message=%r", message)
    origin, dest = _extract_route_free_text(message)
    if not origin or not dest:
        return UI_TEXT.get("ask_trip_route", "❓ Πες μου από πού ξεκινάς και πού πας (π.χ. 'από Πάτρα μέχρι Λουτράκι').")

def _price_band(eur: float, pct: float = 0.08) -> tuple[int, int]:
    low = eur * (1 - pct)
    high = eur * (1 + pct)
    # στρογγύλεψε στο πλησιέστερο 5€
    def r5(x): return int(round(x / 5.0)) * 5
    return max(0, r5(low)), r5(high)

    # 1) Προσπάθησε Timologio
    data = {"error": "unavailable"}
    if TimologioClient is not None:
        try:
            client = TimologioClient()
            data = client.estimate_trip(origin, dest, when=when)
            logger.info("[tool] timologio response: %s", data)
        except Exception:
            logger.exception("[tool] timologio call failed")

    # === SUCCESS PATH (Timologio OK) ===
    if isinstance(data, dict) and "error" not in data:
        price = (
            data.get("price_eur")
            or data.get("price")
            or data.get("total_eur")
            or data.get("fare")
        )
        dist = data.get("distance_km") or data.get("km") or data.get("distance")
    
    # ... μέσα στο success path του trip_quote_nlp:
    if price is not None:
        try:
            price_val = float(str(price).replace(",", "."))
            lo, hi = _price_band(price_val, pct=0.08)
            parts.append(f"💶 Εκτίμηση: {lo}–{hi}€")
        except Exception:
            parts.append(f"💶 Εκτίμηση: ~{price}€")

        # duration: λεπτά / seconds / HH:MM / ISO "PT..."
        raw_dur = (
            data.get("duration_min")
            or data.get("minutes")
            or data.get("duration")
            or data.get("duration_seconds")
        )
        mins = _normalize_minutes(raw_dur, distance_km=dist)
        dur_text = _fmt_minutes(mins) if mins is not None else None

        # map_url να υπάρχει στο ΚΕΙΜΕΝΟ (για να βγει το κουμπί στο UI)
        map_url = (
            data.get("map_url") or data.get("mapLink") or
            data.get("route_url") or data.get("map")
        )

        parts = []
        if price is not None: parts.append(f"💶 Εκτίμηση: ~{price}€")
        if dist  is not None:
            try:
                parts.append(f"🛣️ Απόσταση: ~{round(float(dist), 2)} km")
            except Exception:
                parts.append(f"🛣️ Απόσταση: ~{dist} km")
        if dur_text is not None: parts.append(f"⏱️ Χρόνος: ~{dur_text}")
        if map_url: parts.append(f"📌 Δες τη διαδρομή στον χάρτη: {map_url}")
        parts.append("⚠️ Η τιμή δεν περιλαμβάνει διόδια.")
        return "\n".join(parts)

    # === FALLBACK (Timologio down) ===
    logger.warning("[tool] timologio unavailable, using fallback")
    dist = _rough_distance_km(origin, dest)
    est = _estimate_price_and_time_km(dist)
    dur_text = _fmt_minutes(est["duration_min"])
    map_url = f"https://www.google.com/maps/dir/?api=1&origin={quote_plus(origin)}&destination={quote_plus(dest)}&travelmode=driving"
    lo, hi = _price_band(est['price_eur'], pct=0.08)
    return (
        f"💶 Εκτίμηση: {lo}–{hi}€\n"
        f"🛣️ Απόσταση: ~{est['distance_km']} km\n"
        f"⏱️ Χρόνος: ~{dur_text}\n"
        f"📌 Δες τη διαδρομή στον χάρτη: {map_url}\n"
        f"{UI_TEXT.get('fare_disclaimer','⚠️ Η τιμή δεν περιλαμβάνει διόδια.')}"
    )

@function_tool
def trip_estimate(origin: str, destination: str) -> str:
    try:
        dist = _rough_distance_km(origin, destination)
        est = _estimate_price_and_time_km(dist)
        return (
            f"💶 Εκτίμηση: ~{est['price_eur']}€\n"
            f"🛣️ Απόσταση: ~{est['distance_km']} km\n"
            f"⏱️ Χρόνος: ~{_fmt_minutes(est['duration_min'])}\n"
            f"⚠️ Η τιμή δεν περιλαμβάνει διόδια."
        )
    except Exception:
        logger.exception("trip_estimate failed")
        return "❌ Δεν μπόρεσα να υπολογίσω την εκτίμηση."

# ──────────────────────────────────────────────────────────────────────────────
# Επαφές Taxi (από constants με fallback σε .env)
TAXI_EXPRESS_PHONE = _brand("phone", "TAXI_EXPRESS_PHONE") or "2610 450000"
TAXI_SITE_URL      = _brand("site_url", "TAXI_SITE_URL") or "https://taxipatras.com"
TAXI_BOOKING_URL   = _brand("booking_url", "TAXI_BOOKING_URL") or "https://booking.infoxoros.com/?key=cbe08ae5-d968-43d6-acba-5a7c441490d7"
TAXI_APP_URL       = _brand("app_url", "TAXI_APP_URL") or "https://grtaxi.eu/OsiprERdfdfgfDcfrpod"  # optional

@function_tool
def taxi_contact(city: str = "Πάτρα") -> str:
    city_l = (city or "").lower()
    if any(x in city_l for x in ["πάτρα", "patra", "patras"]):
        lines = [
            f"📞 Τηλέφωνο: {TAXI_EXPRESS_PHONE}",
            f"🌐 Ιστότοπος: {TAXI_SITE_URL}",
            f"🧾 Online κράτηση: {TAXI_BOOKING_URL}",
        ]
        if TAXI_APP_URL:
            lines.append(f"📱 Εφαρμογή: {TAXI_APP_URL}")
        lines.append("🚖 Εναλλακτικά: Καλέστε στο 2610450000")
        return "\n".join(lines)
    return f"🚖 Δεν έχω ειδικά στοιχεία για {city}. Θέλεις να καλέσω τοπικά ραδιοταξί;"

# ──────────────────────────────────────────────────────────────────────────────
# Φαρμακεία

# === ΝΕΟ: Χτίσε κανόνες από constants.AREA_ALIASES δυναμικά
def _build_area_rules():
    rules = []
    aliases_map = AREA_ALIASES or {}
    for canon, aliases in aliases_map.items():
        norm_aliases = [re.escape(_norm_txt(a)) for a in aliases if a]
        if not norm_aliases:
            continue
        pattern = r"\b(?:" + "|".join(norm_aliases) + r")\b"
        rules.append((pattern, canon))
    return rules

AREA_RULES: List[tuple[str, str]] = _build_area_rules()

DEFAULT_AREA = DEFAULTS.get("default_area", "Πάτρα")

def _area_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    t = _norm_txt(text)

    # 1) regex κανόνες από aliases
    for pat, canon in AREA_RULES:
        if re.search(pat, t):
            return canon

    # 2) “στο/στη/στα …” → ξαναδοκίμασε πάνω στα ίδια patterns
    m = re.search(r"\bστ[οην]\s+([a-z0-9 .'\-]+)", t)
    if m:
        chunk = m.group(1).strip()
        for pat, canon in AREA_RULES:
            if re.search(pat, chunk):
                return canon

    return None

@function_tool
def pharmacy_lookup(area: str = DEFAULT_AREA, method: str = "get") -> str:
    if PharmacyClient is None:
        return "❌ PharmacyClient δεν είναι διαθέσιμος."
    client = PharmacyClient()
    try:
        data = client.get_on_duty(area=area, method=method)
    except Exception:
        logger.exception("pharmacy_lookup failed")
        return UI_TEXT.get("generic_error", "❌ Δεν κατάφερα να φέρω εφημερεύοντα φαρμακεία.")

    items = data if isinstance(data, list) else data.get("pharmacies", [])
    if not items:
        return UI_TEXT.get("pharmacy_none_for_area", "❌ Δεν βρέθηκαν εφημερεύοντα.").format(area=area)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for p in items:
        tr = (p.get("time_range") or "Ώρες μη διαθέσιμες").strip()
        groups.setdefault(tr, []).append(p)

    def _start_minutes(s: str) -> int:
        m = re.search(r"(\d{1,2}):(\d{2})", s)
        if not m:
            return 10_000
        return int(m.group(1)) * 60 + int(m.group(2))

    sorted_ranges = sorted(groups.keys(), key=_start_minutes)

    lines: List[str] = []
    for tr in sorted_ranges:
        lines.append(f"**{tr}**")
        for p in groups[tr]:
            name = (p.get("name") or "—").strip()
            addr = (p.get("address") or "—").strip()
            lines.append(f"{name} — {addr}")
        lines.append("")
    return "\n".join(lines).strip()

@function_tool
def pharmacy_lookup_nlp(message: str, method: str = "get") -> str:
    if PharmacyClient is None:
        return "❌ PharmacyClient δεν είναι διαθέσιμος."

    area = _area_from_text(message)  # <-- ΧΩΡΙΣ default εδώ
    if not area:
        return UI_TEXT.get("ask_pharmacy_area",
                           "Για ποια περιοχή να ψάξω εφημερεύον φαρμακείο; 😊")

    client = PharmacyClient()
    try:
        data = client.get_on_duty(area=area, method=method)
    except Exception:
        logger.exception("pharmacy_lookup_nlp failed")
        return UI_TEXT.get("generic_error", "❌ Δεν κατάφερα να φέρω εφημερεύοντα φαρμακεία.")

    items = data if isinstance(data, list) else data.get("pharmacies", [])
    if not items:
        return UI_TEXT.get("pharmacy_none_for_area", "❌ Δεν βρέθηκαν εφημερεύοντα για {area}.").format(area=area)

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for p in items:
        tr = (p.get("time_range") or "Ώρες μη διαθέσιμες").strip()
        groups.setdefault(tr, []).append(p)

    def _start_minutes(s: str) -> int:
        m = re.search(r"(\d{1,2}):(\d{2})", s)
        if not m:
            return 10_000
        return int(m.group(1)) * 60 + int(m.group(2))

    sorted_ranges = sorted(groups.keys(), key=_start_minutes)

    lines: List[str] = [f"**Περιοχή: {area}**"]
    for tr in sorted_ranges:
        lines.append(f"**{tr}**")
        for p in groups[tr]:
            name = (p.get("name") or "—").strip()
            addr = (p.get("address") or "—").strip()
            lines.append(f"{name} — {addr}")
        lines.append("")
    return "\n".join(lines).strip()

# ──────────────────────────────────────────────────────────────────────────────
# Νοσοκομεία / Γενικές Πάτρας
@function_tool
def hospital_duty(which_day: str = "σήμερα") -> str:
    if HospitalsClient is None:
        return "❌ HospitalsClient δεν είναι διαθέσιμος."
    client = HospitalsClient()
    return client.which_hospital(which_day=which_day)

@function_tool
def patras_info(query: str) -> str:
    if PatrasAnswersClient is None:
        return "❌ PatrasAnswersClient δεν είναι διαθέσιμος."
    client = PatrasAnswersClient()
    return client.ask(query)

# Εξαγωγή helper για το main (2-βημα flow φαρμακείων)
def detect_area_for_pharmacy(message: str):
    try:
        return _area_from_text(message)
    except Exception:
        return None
