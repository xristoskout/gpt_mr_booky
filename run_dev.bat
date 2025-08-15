# file: tools.py
from __future__ import annotations

import os
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import unicodedata
from urllib.parse import quote_plus

from agents import function_tool, RunContextWrapper  # Agents SDK types
from unicodedata import normalize as _u_norm

try:
    from openai import OpenAI
except Exception:  # optional dependency
    OpenAI = None  # type: ignore

from phrases import pick_trendy_phrase
from constants import TAXI_TARIFF
import constants

# ──────────────────────────────────────────────────────────────────────────────
# Module logger (no global basicConfig here)
logger = logging.getLogger(__name__)

# Safe getattr ώστε να μην σκάει αν λείπει κάτι
BRAND_INFO: Dict[str, Any] = getattr(constants, "BRAND_INFO", {})
DEFAULTS: Dict[str, Any] = getattr(constants, "DEFAULTS", {})
UI_TEXT: Dict[str, str] = getattr(constants, "UI_TEXT", {})
AREA_ALIASES: Dict[str, List[str]] = getattr(constants, "AREA_ALIASES", {})

# Q tails που κολλάνε στο destination (GR & greeklish)
_Q_TAIL_GR = r"(?:\bπόσο(?:\s+κοστίζει)?\b|\bκοστίζει\b|\bκάνει\b|\bτιμή\b|\?)\s*$"
_Q_TAIL_GL = r"(?:\bposo(?:\s+kostizei)?\b|\bkostizei\b|\bkanei\b|\btimi\b|\?)\s*$"

# Stopwords που ΔΕΝ πρέπει να θεωρηθούν origin/destination
_ROUTE_STOPWORDS = {"πόσο", "κοστίζει", "κάνει", "τιμή", "poso", "kostizei", "kanei", "timi"}

# ──────────────────────────────────────────────────────────────────────────────
# Helpers: text normalization

def _deaccent(s: str) -> str:
    if not s:
        return ""
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def _norm_txt(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = _deaccent(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _preclean_route_text(s: str) -> str:
    """Καθαρίζει καταλήξεις ερώτησης και greeklish connectives για parsing."""
    s = (s or "").strip()
    s = re.sub(_Q_TAIL_GR, "", s, flags=re.IGNORECASE)
    s = re.sub(_Q_TAIL_GL, "", s, flags=re.IGNORECASE)
    repl = {
        r"\bapo\b": "από",
        r"\bapó\b": "από",
        r"\bmexri\b": "μέχρι",
        r"\bmehri\b": "μέχρι",
        r"\bpros\b": "προς",
        r"\bgia\b": "για",
        r"\beos\b": "έως",
        r"\bews\b": "έως",
        r"\bfrom\b": "από",
        r"\bto\b": "προς",
    }
    for pat, rep in repl.items():
        s = re.sub(pat, rep, s, flags=re.IGNORECASE)
    s = _u_norm("NFKC", s)
    return s.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Brand info (fallback: env)

def _brand(key: str, env_fallback: Optional[str] = None) -> str:
    val = BRAND_INFO.get(key)
    if val:
        return str(val)
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
    PharmacyClient = HospitalsClient = PatrasAnswersClient = TimologioClient = None  # type: ignore


# ──────────────────────────────────────────────────────────────────────────────
# LLM helper με system prompt από context

def _ask_llm_with_system_prompt(
    user_message: str,
    system_prompt: str,
    context_text: str = "",
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    if OpenAI is None:
        return UI_TEXT.get("generic_error", "❌ LLM client δεν είναι διαθέσιμος.")
    model = os.getenv("LLM_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))

    client = OpenAI()

    history_msgs: List[Dict[str, str]] = []
    if history:
        for h in history[-2:]:  # περιορίζουμε PII & κόστος
            if h.get("user"):
                history_msgs.append({"role": "user", "content": h["user"]})
            if h.get("bot"):
                history_msgs.append({"role": "assistant", "content": h["bot"]})

    messages: List[Dict[str, str]] = (
        [{"role": "system", "content": system_prompt}]
        + history_msgs
        + [{"role": "user", "content": f"{user_message}\n\n[Context]\n{context_text}"}]
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.7,
            presence_penalty=0.6,
            frequency_penalty=0.2,
        )
        return resp.choices[0].message.content or ""
    except Exception:
        logger.exception("ask_llm OpenAI call failed")
        return UI_TEXT.get("generic_error", "❌ Παρουσιάστηκε σφάλμα κατά την κλήση του LLM.")


@function_tool(
    name_override="ask_llm",
    description_override="Στέλνει μήνυμα στο LLM με system prompt από το context, μαζί με optional context_text & history.",
)
def ask_llm(ctx: RunContextWrapper[Any], user_message: str) -> str:
    try:
        c: Dict[str, Any] = ctx.context or {}

        # Αν υπάρχει explicit desired_tool και ΔΕΝ είναι το ask_llm, μην απαντήσεις από εδώ.
        desired = c.get("desired_tool")
        if desired and desired != "ask_llm":
            return "⏭️"

        system_prompt = c.get("system_prompt") or "You are a helpful assistant."
        context_text = c.get("context_text") or ""
        history = c.get("history") or []

        return _ask_llm_with_system_prompt(
            user_message=user_message,
            system_prompt=system_prompt,
            context_text=context_text,
            history=history,
        )
    except Exception:
        logger.exception("ask_llm failed")
        return UI_TEXT.get("generic_error", "❌ Παρουσιάστηκε σφάλμα κατά την κλήση του LLM.")


# ──────────────────────────────────────────────────────────────────────────────
# Rough distance fallback

def _rough_distance_km(origin: str, destination: str) -> float:
    known = {
        ("πάτρα", "αθήνα"): 275.0,
        ("patra", "athens"): 275.0,
        ("πάτρα", "πρέβεζα"): 220.0,
        ("πάτρα", "καλαμάτα"): 210.0,
        ("πάτρα", "λουτράκι"): 184.0,
    }
    key = (origin.lower().strip(), destination.lower().strip())
    return float(known.get(key, 200.0))


def _estimate_price_and_time_km(distance_km: float, *, night: bool = False) -> Dict[str, Any]:
    """Χονδρική εκτίμηση κόστους/χρόνου απόστασης (χωρίς διόδια)."""
    start_fee = float(TAXI_TARIFF.get("minimum_fare", 4.0))
    day_km = float(TAXI_TARIFF.get("km_rate_city_or_day", TAXI_TARIFF.get("km_rate_zone1", 0.9)))
    night_km = float(TAXI_TARIFF.get("km_rate_zone2_or_night", max(day_km, 1.25)))
    per_km = night_km if night else day_km
    avg_kmh = 85.0
    duration_h = distance_km / max(avg_kmh, 1.0)
    duration_min = int(round(duration_h * 60))
    cost = start_fee + per_km * max(distance_km, 0.0)
    return {"distance_km": round(distance_km, 1), "duration_min": duration_min, "price_eur": round(cost, 2)}


def _round5(eur: float) -> int:
    """Στρογγυλοποίηση στο πλησιέστερο 5€ (προς το κοντινότερο)."""
    try:
        x = float(eur)
    except Exception:
        return int(eur) if isinstance(eur, int) else 0
    return int(round(x / 5.0)) * 5


# ──────────────────────────────────────────────────────────────────────────────
# NLP parsing για διαδρομές


def _detect_night_or_double_tariff(message: str, when: str) -> bool:
    t = (message or "").lower()
    if re.search(r"νυχτ|διπλ|double|night", t):
        return True
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", (when or "").lower())
    if m:
        hh = int(m.group(1))
        return 0 <= hh < 5
    return False


def _extract_route_free_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (origin, destination) από ελεύθερο κείμενο."""
    s = _preclean_route_text(text)
    if not s:
        return None, None

    m = re.search(r"\bαπό\s+(?P<o>.+?)\s+(?:μέχρι|έως|προς|για)\s+(?P<d>.+)$", s, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"^(?P<o>.+?)\s+(?:προς|για)\s+(?P<d>.+)$", s, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"^(?P<d>.+?)\s+από\s+(?P<o>.+)$", s, flags=re.IGNORECASE)

    if not m:
        return None, None

    o = (m.group("o") or "").strip(" ,.;·") if "o" in m.groupdict() else None
    d = (m.group("d") or "").strip(" ,.;·") if "d" in m.groupdict() else None

    if d and (d.lower() in _ROUTE_STOPWORDS or len(d) <= 1):
        d = None

    return (o or None), (d or None)


def _normalize_minutes(val: Any, distance_km: Optional[float] = None) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        m = int(round(val))
        if m > 1800:  # πιθανότατα seconds
            m = int(round(m / 60))
        return max(m, 0)

    s = str(val).strip().lower()
    ms = re.match(r"^(\d+)\s*s$", s)
    if ms:
        return int(ms.group(1)) // 60

    mm = re.search(r"\b(\d{1,3})[:.](\d{2})\b", s)
    if mm:
        return int(mm.group(1)) * 60 + int(mm.group(2))

    if s.startswith("pt"):
        h = re.search(r"(\d+)h", s)
        m = re.search(r"(\d+)m", s)
        sec = re.search(r"(\d+)s", s)
        mins = 0
        if h:
            mins += int(h.group(1)) * 60
        if m:
            mins += int(m.group(1))
        if sec:
            mins += int(sec.group(1)) // 60
        if mins:
            return mins

    m1 = re.search(r"(\d+)\s*ώρ", s)
    m2 = re.search(r"(\d+)\s*λεπ", s)
    if m1 and m2:
        return int(m1.group(1)) * 60 + int(m2.group(1))
    if m2:
        return int(m2.group(1))
    if s.isdigit():
        return int(s)

    if distance_km:
        approx = int(round((float(distance_km) / 85.0) * 60))
        return max(approx, 0)
    return None


def _fmt_minutes(mins: Optional[int]) -> Optional[str]:
    if mins is None:
        return None
    try:
        m = int(mins)
    except Exception:
        return None
    h, r = divmod(max(m, 0), 60)
    if h and r:
        return f"{h} ώρες και {r} λεπτά"
    if h:
        return f"{h} ώρες"
    return f"{r} λεπτά"


# ──────────────────────────────────────────────────────────────────────────────
# Trip quote tools


@function_tool
def trip_quote_nlp(message: str, when: str = "now") -> str:
    """Βγάζει origin/destination από ελεύθερο κείμενο και καλεί Timologio (αν υπάρχει)."""
    logger.info("[tool] trip_quote_nlp parse")
    origin, dest = _extract_route_free_text(message)
    if not origin or not dest:
        return UI_TEXT.get(
            "ask_trip_route",
            "❓ Πες μου από πού ξεκινάς και πού πας (π.χ. «Από Πάτρα μέχρι Διακοπτό»).",
        )

    night = _detect_night_or_double_tariff(message, when)

    # 1) Timologio
    data: Dict[str, Any] = {"error": "unavailable"}
    if TimologioClient is not None:
        try:
            client = TimologioClient()
            data = client.estimate_trip(origin, dest, when=when)
            logger.debug("[tool] timologio ok: keys=%s", list(data.keys()))
        except Exception:
            logger.exception("[tool] timologio call failed")

    # 2) SUCCESS PATH
    if isinstance(data, dict) and "error" not in data:
        price = data.get("price_eur") or data.get("price") or data.get("total_eur") or data.get("fare")
        dist = data.get("distance_km") or data.get("km") or data.get("distance")
        raw_dur = data.get("duration_min") or data.get("minutes") or data.get("duration") or data.get("duration_seconds")
        mins = _normalize_minutes(raw_dur, distance_km=dist)
        dur_text = _fmt_minutes(mins) if mins is not None else None
        map_url = data.get("map_url") or data.get("mapLink") or data.get("route_url") or data.get("map")
        if not map_url:
            map_url = (
                f"https://www.google.com/maps/dir/?api=1&origin={quote_plus(origin)}&destination={quote_plus(dest)}&travelmode=driving"
            )

        parts: List[str] = []
        if price is None and dist is not None:
            try:
                km_val = float(str(dist).replace("km", "").strip())
                est = _estimate_price_and_time_km(km_val, night=night)
                price = est["price_eur"]
            except Exception:
                price = None

        if price is not None:
            try:
                price_val = float(str(price).replace(",", "."))
                rounded = _round5(price_val)
                parts.append(f"💶 Τιμή: {rounded}€")
            except Exception:
                parts.append(f"💶 Τιμή: {price}€")
        if dist is not None:
            try:
                parts.append(f"🛣️ Απόσταση: ~{round(float(dist), 2)} km")
            except Exception:
                parts.append(f"🛣️ Απόσταση: ~{dist} km")
        if dur_text is not None:
            parts.append(f"⏱️ Χρόνος: ~{dur_text}")
        if map_url:
            parts.append(f"[📌 Δες τη διαδρομή στον χάρτη]({map_url})")
        parts.append(UI_TEXT.get("fare_disclaimer", "⚠️ Η τιμή δεν περιλαμβάνει διόδια."))
        return "\n".join(parts)

    # 3) FALLBACK (Timologio down)
    logger.warning("[tool] timologio unavailable, using fallback")
    dist = _rough_distance_km(origin, dest)
    est = _estimate_price_and_time_km(dist, night=night)
    dur_text = _fmt_minutes(est["duration_min"]) or "—"
    map_url = (
        f"https://www.google.com/maps/dir/?api=1&origin={quote_plus(origin)}&destination={quote_plus(dest)}&travelmode=driving"
    )

    rounded = _round5(est["price_eur"])
    body = [
        f"💶 Εκτίμηση: {rounded}€" + (" (νύχτα)" if night else ""),
        f"🛣️ Απόσταση: ~{est['distance_km']} km",
        f"⏱️ Χρόνος: ~{dur_text}",
        f"[📌 Δες τη διαδρομή στον χάρτη]({map_url})",
        UI_TEXT.get("fare_disclaimer", "⚠️ Η τιμή δεν περιλαμβάνει διόδια."),
    ]
    return "\n".join(body)


@function_tool
def trip_estimate(origin: str, destination: str, when: str = "now") -> str:
    try:
        dist = _rough_distance_km(origin, destination)
        night = _detect_night_or_double_tariff("", when)
        est = _estimate_price_and_time_km(dist, night=night)
        rounded = _round5(est["price_eur"])
        map_url = (
            f"https://www.google.com/maps/dir/?api=1&origin={quote_plus(origin)}&destination={quote_plus(destination)}&travelmode=driving"
        )
        return (
            f"💶 Εκτίμηση: {rounded}€" + (" (νύχτα)" if night else "") + "\n"
            f"🛣️ Απόσταση: ~{est['distance_km']} km\n"
            f"⏱️ Χρόνος: ~{_fmt_minutes(est['duration_min'])}\n"
            f"[📌 Δες τη διαδρομή στον χάρτη]({map_url})\n"
            f"⚠️ Η τιμή δεν περιλαμβάνει διόδια."
        )
    except Exception:
        logger.exception("trip_estimate failed")
        return "❌ Δεν μπόρεσα να υπολογίσω την εκτίμηση."


# ──────────────────────────────────────────────────────────────────────────────
# Επαφές Taxi (από constants με fallback σε .env) — χωρίς σκληροκωδικές URLs

TAXI_EXPRESS_PHONE = _brand("phone", "TAXI_EXPRESS_PHONE") or "2610 450000"
TAXI_SITE_URL = _brand("site_url", "TAXI_SITE_URL") or "https://taxipatras.com"
TAXI_BOOKING_URL = _brand("booking_url", "TAXI_BOOKING_URL")  # no insecure fallback
TAXI_APP_URL = _brand("app_url", "TAXI_APP_URL")  # optional; no insecure fallback


@function_tool
def taxi_contact(city: str = "Πάτρα") -> str:
    city_l = (city or "").lower()
    if any(x in city_l for x in ["πάτρα", "patra", "patras"]):
        lines = [
            f"📞 Τηλέφωνο: {TAXI_EXPRESS_PHONE}",
            f"🌐 Ιστότοπος: {TAXI_SITE_URL}",
        ]
        if TAXI_BOOKING_URL:
            lines.append(f"🧾 Online κράτηση: {TAXI_BOOKING_URL}")
        if TAXI_APP_URL:
            lines.append(f"📱 Εφαρμογή: {TAXI_APP_URL}")
        lines.append("🚖 Εναλλακτικά: Καλέστε στο 2610450000")
        return "\n".join(lines)
    return f"🚖 Δεν έχω ειδικά στοιχεία για {city}. Θέλεις να καλέσω τοπικά ραδιοταξί;"


# ──────────────────────────────────────────────────────────────────────────────
# Φαρμακεία


def _build_area_rules() -> List[Tuple[str, str]]:
    rules: List[Tuple[str, str]] = []
    aliases_map = AREA_ALIASES or {}
    for canon, aliases in aliases_map.items():
        norm_aliases = [re.escape(_norm_txt(a)) for a in aliases if a]
        if not norm_aliases:
            continue
        pattern = r"\b(?:" + "|".join(norm_aliases) + r")\b"
        rules.append((pattern, canon))
    return rules


AREA_RULES: List[Tuple[str, str]] = _build_area_rules()
DEFAULT_AREA = DEFAULTS.get("default_area", "Πάτρα")


def _area_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    t = _norm_txt(text)

    for pat, canon in AREA_RULES:
        if re.search(pat, t):
            return canon

    m = re.search(r"\bστ[οην]\s+([a-z0-9 .'\-]+)", t)
    if m:
        chunk = m.group(1).strip()
        for pat, canon in AREA_RULES:
            if re.search(pat, chunk):
                return canon

    return None


@function_tool(
    name_override="trendy_phrase",
    description_override="Επιλέγει μια trend φράση βάσει emotion/context/lang/season.",
)
def trendy_phrase(emotion: str = "joy", context: str = "success", lang: str = "el", season: str = "all") -> str:
    t = pick_trendy_phrase(emotion=emotion, context=context, lang=lang, season=season)
    return t or ""


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
        m_ = re.search(r"(\d{1,2}):(\d{2})", s or "")
        if not m_:
            return 10_000
        return int(m_.group(1)) * 60 + int(m_.group(2))

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

    area = _area_from_text(message)  # no default εδώ
    if not area:
        return UI_TEXT.get("ask_pharmacy_area", "Για ποια περιοχή να ψάξω εφημερεύον φαρμακείο; 😊")

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
        m_ = re.search(r"(\d{1,2}):(\d{2})", s or "")
        if not m_:
            return 10_000
        return int(m_.group(1)) * 60 + int(m_.group(2))

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
    try:
        return client.which_hospital(which_day=which_day)
    except Exception:
        logger.exception("hospital_duty failed")
        return UI_TEXT.get("generic_error", "❌ Δεν κατάφερα να φέρω τα εφημερεύοντα νοσοκομεία.")


@function_tool
def patras_info(query: str) -> str:
    if PatrasAnswersClient is None:
        return "❌ PatrasAnswersClient δεν είναι διαθέσιμος."
    client = PatrasAnswersClient()
    try:
        return client.ask(query)
    except Exception:
        logger.exception("patras_info failed")
        return UI_TEXT.get("generic_error", "❌ Δεν κατάφερα να βρω πληροφορίες.")


# Εξαγωγή helper για το main (2-βημα flow φαρμακείων)

def detect_area_for_pharmacy(message: str):
    try:
        return _area_from_text(message)
    except Exception:
        return None
