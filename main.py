# file: main.py
from __future__ import annotations
import os
import logging
import re
import random
import asyncio
import threading
import uuid
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

try:
    from agents import Agent, Runner, function_tool  # type: ignore
    HAS_AGENTS_SDK = True
except Exception:  # graceful fallback when Agents SDK is missing
    HAS_AGENTS_SDK = False
    def function_tool(fn=None, **kwargs):
        # Minimal no-op decorator to let tools load without Agents SDK
        def _decorator(f):
            # attach a .name attribute similar to agents SDK for consistency
            setattr(f, "name", kwargs.get("name_override", getattr(f, "__name__", "tool")))
            return f
        if fn is None:
            return _decorator
        return _decorator(fn)

    class Agent:  # minimal placeholder
        def __init__(self, *args, **kwargs): 
            self.name = kwargs.get("name", "agent")
            self.tools = kwargs.get("tools", [])
            self.instructions = kwargs.get("instructions", "")

    class Runner:  # minimal placeholder runner: we won't actually use it
        @staticmethod
        async def run(agent, input: str, context: dict):
            raise RuntimeError("Agents SDK not installed; using direct tool dispatch fallback.")
from config import Settings
from dataclasses import dataclass, field, asdict
import constants
from api_clients import PharmacyClient

from unicodedata import normalize as _u_norm
import time
from constants import TOUR_PACKAGES
from security import APIKeyAuthMiddleware, RateLimitMiddleware, BodySizeLimitMiddleware
from constants import TAXI_TARIFF as _TT

# εργαλεία
from tools import (
    taxi_contact,
    trip_estimate,
    pharmacy_lookup,
    pharmacy_lookup_nlp,
    hospital_duty,
    patras_info,
    trip_quote_nlp,
    trendy_phrase,
    ask_llm,
    detect_area_for_pharmacy,
)
from tools import RunContextWrapper as _RunCtx
# 🔹 ΝΕΟ: LLM Router & Booking helpers
from router_and_booking import (
    init_session_state,
    maybe_handle_followup_or_booking,
)

# ──────────────────────────────────────────────────────────────────────────────
# .env + settings
load_dotenv()
settings = Settings()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Taxi Agent")

# Size guard (413)
app.add_middleware(BodySizeLimitMiddleware, max_body_bytes=getattr(settings, "MAX_BODY_BYTES", 1_000_000))

# --- CORS από env (fallback στο default) ---

def _parse_origins() -> set[str]:
    env_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    cfg = getattr(settings, "ALLOWED_ORIGINS", []) or []
    return set(env_origins or cfg or ["http://localhost:3000"])  # sensible default for dev


ALLOWED_ORIGINS = _parse_origins()
ALLOWED_ORIGIN_REGEX = os.getenv("ALLOWED_ORIGIN_REGEX", getattr(settings, "ALLOWED_ORIGIN_REGEX", None) or None)
_ORIGIN_RE = re.compile(ALLOWED_ORIGIN_REGEX) if ALLOWED_ORIGIN_REGEX else None

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_origin_regex=ALLOWED_ORIGIN_REGEX,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=600,
)

# 🔐 API key + rate limit as middlewares (ΟΧΙ Depends σε routes)
_KEYS = [k.strip() for k in os.getenv("CHAT_API_KEYS", "").split(",") if k.strip()]
app.add_middleware(APIKeyAuthMiddleware, keys=_KEYS)
app.add_middleware(RateLimitMiddleware, settings=settings)


class _PreflightMiddleware:
    """Unconditionally handle CORS preflight before auth/ratelimit.
    Must be added *after* auth/rl so it runs first (FastAPI wraps last-added first).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("method") == "OPTIONS":
            req = Request(scope, receive)
            origin = req.headers.get("origin", "")
            req_headers = req.headers.get("access-control-request-headers", "Content-Type, Authorization")
            headers = _cors_headers(origin)
            if headers:
                headers.setdefault("Access-Control-Allow-Headers", req_headers)
                headers.setdefault("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
                # no body for 204
                res = Response(status_code=204, headers=headers)
                return await res(scope, receive, send)
        return await self.app(scope, receive, send)


# Add preflight bypass LAST so it executes FIRST
app.add_middleware(_PreflightMiddleware)


def _cors_headers(origin: str) -> dict:
    if not origin:
        return {}
    allowed = origin in ALLOWED_ORIGINS or (_ORIGIN_RE.match(origin) if _ORIGIN_RE else False)
    if allowed:
        return {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Max-Age": "600",
        }
    return {}

# ──────────────────────────────────────────────────────────────────────────────
# Cancel/Confirm regex (αποφεύγουμε false positives)
CANCEL_RE = re.compile(r"^(?:άκυρο|ακυρο|cancel|τέλος|τελος|σταμάτα|σταματα|stop)\.?$", re.IGNORECASE | re.UNICODE)


def is_cancel_message(text: str) -> bool:
    t = (text or "").strip()
    return bool(len(t) <= 16 and CANCEL_RE.match(t))

# Επιβεβαιώσεις τύπου "ναι/σωστά/ok" για να ΜΗΝ κάνουμε reset intent
CONFIRM_RE = re.compile(r"\b(ναι|ναι\.?|σωστά|σωστα|ok|οκ|έτσι|ακριβώς)\b", re.IGNORECASE | re.UNICODE)

# Intents (προαιρετικά classifier)
try:
    from intents import IntentClassifier

    INTENT_CLF = IntentClassifier()

    def predict_intent(text: str):
        if hasattr(INTENT_CLF, "classify"):
            return INTENT_CLF.classify(text)
        if hasattr(INTENT_CLF, "predict"):
            return INTENT_CLF.predict(text)
        return (None, 0.0)
except Exception:
    INTENT_CLF = None

    def predict_intent(_):
        return (None, 0.0)

SYSTEM_PROMPT = constants.SYSTEM_PROMPT

# ──────────────────────────────────────────────────────────────────────────────
# Μοντέλα
class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"
    session_id: Optional[str] = None
    context: Optional[str] = None
    history: Optional[List[Dict[str, str]]] = None

# ──────────────────────────────────────────────────────────────────────────────
# Tools για Agent

def ensure_tool(t):
    return t if hasattr(t, "name") else function_tool(t)


tool_candidates = [
    ask_llm,
    trip_quote_nlp,
    trip_estimate,
    pharmacy_lookup,
    pharmacy_lookup_nlp,
    hospital_duty,
    patras_info,
    taxi_contact,
    trendy_phrase,
]
TOOLS = [ensure_tool(t) for t in tool_candidates]
for t in TOOLS:
    logger.info("🔧 tool loaded: %s (%s)", getattr(t, "name", t), type(t))

chat_agent = Agent(
    name="customer_support_agent",
    instructions=(
        "Είσαι ο Mr Booky ,ένας ζεστός, χιουμοριστικός agent εξυπηρέτησης πελατών του Taxi Express Patras. "
        "Απάντα στα ελληνικά, χρησιμοποιώντας φυσικές, πλήρεις προτάσεις. "
        "Μην συλλαβίζεις, μην προφέρεις λέξεις γράμμα-γράμμα, και απέφυγε τεχνικές περιγραφές. "
        "Η απάντηση πρέπει να είναι έτοιμη για προφορική εκφώνηση (text-to-speech), σαν να μιλάς σε τηλέφωνο. "
        "Χρησιμοποίησε απλή, καθημερινή γλώσσα."
        "Αν στο context υπάρχει `desired_tool`, προσπάθησε πρώτα να καλέσεις αυτό το εργαλείο. "
        "Για κόστος/χρόνο διαδρομών: trip_quote_nlp. "
        "Για φαρμακεία: pharmacy_lookup / pharmacy_lookup_nlp. "
        "Για νοσοκομεία: hospital_duty (σήμερα/αύριο). "
        "Για πληροφορίες/εκδρομές/τοπικά: patras_info. "
        "Για στοιχεία επικοινωνίας ταξί: taxi_contact. "
        "Χρησιμοποίησε ask_llm όταν χρειάζεσαι ελεύθερο reasoning με system prompt."
        "Αν στο context υπάρχει `desired_tool`, ΚΑΛΕΙΣ ΜΟΝΟ αυτό το εργαλείο. "
        "Δεν χρησιμοποιείς ask_llm εκτός αν `desired_tool == 'ask_llm'`."
    ),
    tools=TOOLS,
)

# ──────────────────────────────────────────────────────────────────────────────
# Heuristics / Patterns
INTENT_TOOL_MAP = {
    "TripCostIntent": "trip_quote_nlp",
    "ContactInfoIntent": "taxi_contact",
    "OnDutyPharmacyIntent": "pharmacy_lookup_nlp",
    "HospitalIntent": "hospital_duty",
    "PatrasLlmAnswersIntent": "patras_info",
    "ServicesAndToursIntent": "__internal_services__",
}

CONTACT_PAT = re.compile(
    r"("
    r"(?:ταξι|taxi|radio\s*taxi|taxi\s*express|taxipatras|ραδιοταξι).*"
    r"(?:τηλ|τηλέφων|επικοινων|μαιλ|mail|booking|app|εφαρμογ|site|σελίδ|κατέβασ|install)"
    r"|(?:τηλ|τηλέφων).*(?:ταξι|taxi|taxi\s*express|taxipatras|ραδιοταξι)"
    r"|(?:\bεφαρμογ(?:ή|η)\b|\bapp\b|\bgoogle\s*play\b|\bapp\s*store\b)"
    r")",
    re.IGNORECASE | re.UNICODE,
)


def is_contact_intent(text: str) -> bool:
    return bool(CONTACT_PAT.search(text or ""))


TIME_RANGE_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\s*[-–]\s*\d{1,2}[:.]\d{2}\b")

TRIP_PAT = re.compile(
    r"(πόσο|κοστίζει|τιμή|ταξί|ταξι|fare|cost).*(από).*(μέχρι|προς|για)", re.IGNORECASE | re.DOTALL
)
KM_QUERY_RE = re.compile(r"πόσα\s+χιλιόμετρα", re.IGNORECASE)


def is_trip_quote(text: str) -> bool:
    t = (text or "").lower()
    if TIME_RANGE_RE.search(t):
        return False
    movement_kw = any(w in t for w in ["πάω", "πάμε", "μετάβαση", "διαδρομή", "route", "ταξ", "κοστίζει", "τιμή"])
    basic = bool(TRIP_PAT.search(t)) or KM_QUERY_RE.search(t) is not None or (
        movement_kw and "από" in t and any(w in t for w in ["μέχρι", "προς", "για"]))
    if basic:
        return True
    if ("τιμή" in t or "κόστος" in t) and re.search(r"\bγια\s+[^\d]", t):
        return True
    if re.search(r"πόσο\s+(?:κοστίζει|κάνει|πάει)\s+", t):
        return True
    return False


# --- render helpers για φαρμακεία ---
def _render_pharmacies_text(items: list[dict], area: str) -> str:
    if not items:
        return f"❌ Δεν βρέθηκαν εφημερεύοντα για {area}."

    # Ομαδοποίηση ανά time_range και ταξινόμηση κατά ώρα έναρξης αν υπάρχει
    groups: dict[str, list[dict]] = {}
    for it in items:
        zone = (it.get("time_range") or "—").strip()
        groups.setdefault(zone, []).append(it)

    def _zone_key(z: str) -> int:
        m = re.match(r"(\d{1,2})[:.](\d{2})", z)
        if not m:
            return 99999
        return int(m.group(1)) * 60 + int(m.group(2))

    ordered = sorted(groups.items(), key=lambda kv: _zone_key(kv[0]))

    lines: list[str] = []
    for zone, lst in ordered:
        if zone and zone != "—":
            lines.append(f"🕘 {zone}")
        for p in lst:
            name = (p.get("name") or "Φαρμακείο").strip()
            addr = (p.get("address") or "").strip()
            lines.append(f"• {name}" + (f" — {addr}" if addr else ""))
        lines.append("")
    # καθάρισμα κενών γραμμών στο τέλος
    out = "\n".join([ln for ln in lines if ln.strip()])
    return out


PHARMACY_RE = re.compile(r"\b(φαρμακ|εφημερ)\b", re.IGNORECASE)
HOSPITAL_RE = re.compile(r"\b(νοσοκομ|εφημερ[^\s]*\s*νοσοκομ)\b", re.IGNORECASE)


def is_pharmacy_message(text: str) -> bool:
    return bool(PHARMACY_RE.search(text or ""))


def is_hospital_message(text: str) -> bool:
    return bool(HOSPITAL_RE.search(text or ""))


# --- helpers για slots / km-queries ---

def _missing_slots(intent: str, text: str, st: "SessionState") -> list[str]:
    t = (text or "").lower()
    if intent == INTENT_PHARMACY:
        area = detect_area_for_pharmacy(text) or st.slots.get("area")
        return ["area"] if not area else []
    if intent == INTENT_TRIP:
        try:
            from tools import _extract_route_free_text
        except Exception:
            return []
        o, d = _extract_route_free_text(text)
        if KM_QUERY_RE.search(t):
            return []
        return ["destination"] if not d else []
    return []


def _extract_km_destination(text: str) -> Optional[str]:
    if not KM_QUERY_RE.search(text or ""):
        return None
    s = (text or "").strip()
    s = re.sub(r"[;;?!…]+$", "", s)
    m = re.search(
        r"πόσα\s+χιλιόμετρα(?:\s+είναι)?\s+(?:να\s*πάω\s+)?(?:στην|στον|στο|για|προς|μέχρι|έως)?\s*(.+)$",
        s,
        flags=re.IGNORECASE,
    )
    dest = m.group(1).strip() if m else None
    if not dest:
        m2 = re.search(
            r"^(?:η|ο|το|οι|τα|στην|στον|στο)?\s*([A-Za-zΑ-ΩΆΈΉΊΌΎΏα-ωάέήίϊΐόύϋΰώ.\- ]+)\s+πόσα\s+χιλιόμετρα",
            s,
            flags=re.IGNORECASE,
        )
        if m2:
            dest = m2.group(1).strip()
    if not dest:
        return None
    dest = dest.strip(" .,\u00A0")
    dest = re.sub(r"^(η|ο|οι|το|τα|την|τη|τον|του|της)\s+", "", dest, flags=re.IGNORECASE)
    if re.search(r"\bαπ[όο]\b", dest, flags=re.IGNORECASE):
        return None
    return dest[:80]


def _km_query_to_trip_message(text: str) -> Optional[str]:
    dest = _extract_km_destination(text)
    if dest:
        return f"από Πάτρα μέχρι {dest}"
    return None


def _price_query_to_trip_message(text: str, st: "SessionState") -> Optional[str]:
    s = (text or "").strip()
    s = re.sub(r"[;;?!…]+$", "", s)
    m = re.search(r"^(?:τιμή|τιμη)\s+(?:για\s+)?(.+)$", s, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"^πόσο\s+(?:κοστίζει|κάνει|πάει)\s+(.+)$", s, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"^\b(?:κόστος|κοστος)\b\s+(.+)$", s, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"^για\s+(.+?)\s+(?:τιμή|κόστος|κοστος)$", s, flags=re.IGNORECASE)
    if not m:
        return None
    dest = m.group(1).strip()
    if re.search(r"\b(από|απ[όο])\b", dest, flags=re.IGNORECASE):
        return None
    origin = (st.slots.get("last_origin") or "Πάτρα").strip()
    dest = dest.strip(" .,\u00A0")
    return f"από {origin} μέχρι {dest}"


# μερικό ερώτημα → φτιάξε πλήρη πρόταση διαδρομής

def _partial_to_full_trip(text: str, st: "SessionState") -> Optional[str]:
    s = (text or "").strip()
    m = re.match(r"^(?:μέχρι|προς|για)\s+(.+)$", s, flags=re.IGNORECASE)
    if not m:
        return None
    dest = m.group(1).strip()
    origin = st.slots.get("last_origin") or "Πάτρα"
    return f"από {origin} μέχρι {dest}"


# ──────────────────────────────────────────────────────────────────────────────
# Map link stripper -> UI βγάζει κουμπί
MAP_RE_MD = re.compile(r"\[[^\]]*\]\((https?://www\.google\.com/maps/dir/\?[^)]+)\)")
MAP_RE_RAW = re.compile(r"(https?://www\.google\.com/maps/dir/\?[^ \t\n\r<>]+)")


def strip_map_link(text: str):
    """Extract Google Maps URL and strip only the link token.
    If stripping would empty the message, keep original text."""
    if not text:
        return text, None
    # Markdown link first: [..](https://www.google.com/maps/dir/?..)
    m = MAP_RE_MD.search(text)
    if m:
        url = m.group(1)
        token = m.group(0)  # full markdown token
        cleaned = text.replace(token, "").strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        if not cleaned:
            cleaned = text  # don't drop content entirely
        return cleaned, url
    # Raw URL fallback
    m = MAP_RE_RAW.search(text)
    if m:
        url = m.group(1)
        token = m.group(0)
        cleaned = text.replace(token, "").strip()
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        if not cleaned:
            cleaned = text
        return cleaned, url
    return text, None

# ── Location aliases για δύσκολες ονομασίες (βοηθάει το geocoding)
LOCATION_ALIASES = [
    (re.compile(r"\bάνω\s*χώρα\b", re.IGNORECASE), "Άνω Χώρα Ναυπακτίας"),
]


def _apply_location_aliases(s: str) -> str:
    out = s
    for pat, repl in LOCATION_ALIASES:
        out = pat.sub(repl, out)
    return out


def _contact_reply() -> str:
    brand = getattr(constants, "BRAND_INFO", {}) or {}
    phone = os.getenv("TAXI_EXPRESS_PHONE", brand.get("phone", "2610 450000"))
    site = os.getenv("TAXI_SITE_URL", brand.get("site_url", "https://taxipatras.com"))
    booking = os.getenv("TAXI_BOOKING_URL", brand.get("booking_url", ""))
    appurl = os.getenv("TAXI_APP_URL", brand.get("app_url", ""))
    email = os.getenv("TAXI_EMAIL", brand.get("email", ""))

    lines = [f"📞 Τηλέφωνο: {phone}", f"🌐 Ιστότοπος: {site}"]
    if email:
        lines.append(f"✉️ Email: {email}")
    if booking:
        lines.append(f"🧾 Online κράτηση: {booking}")
    if appurl:
        lines.append(f"📱 Εφαρμογή: {appurl}")
    lines.append("🚖 Εναλλακτικά: Καλέστε μας στο 2610450000")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────

PHRASE_PROB = float(os.getenv("TRENDY_PROB", "0.35"))
PHRASE_COOLDOWN = int(os.getenv("TRENDY_COOLDOWN_SEC", "45"))


def _session_can_phrase(st) -> bool:
    t = time.time()
    last = st.slots.get("last_trendy_ts", 0)
    if (t - last) < PHRASE_COOLDOWN:
        return False
    return random.random() < PHRASE_PROB


def inject_trendy_phrase(text: str, *, st, intent: str, success: bool = True) -> str:
    if not text or not _session_can_phrase(st):
        return text
    context = "success" if success else "fallback"
    emap = {
        "TripCostIntent": "joy" if success else "neutral",
        "OnDutyPharmacyIntent": "joy" if success else "surprise",
        "HospitalIntent": "neutral",
        "PatrasLlmAnswersIntent": "witty" if success else "neutral",
        "ServicesAndToursIntent": "joy" if success else "neutral",
    }
    emotion = emap.get(intent, "neutral")
    try:
        phrase = trendy_phrase(emotion=emotion, context=context, lang="el", season=os.getenv("SEASON", "all"))
    except Exception:
        phrase = None
    if not phrase:
        return text
    st.slots["last_trendy_ts"] = time.time()
    if intent not in ("HospitalIntent",):
        return f"{phrase}\n{text}"
    return text


EMOJI_PACK = {
    "trip": ["🚕", "🛣️", "🕒", "📍", "💶"],
    "pharmacy": ["💊", "🕘", "📍", "🧭"],
    "hospital": ["🏥", "🚑", "🩺"],
    "contact": ["☎️", "🌐", "🧾", "📱"],
    "generic": ["✨", "🙂", "🙌"],
}


def enrich_reply(text: str, intent: Optional[str] = None) -> str:
    t = (text or "").strip()
    if not t:
        return t
    kind = "generic"
    low = t.lower()
    if intent in ("TripCostIntent",) or ("€" in t and "απόσταση" in low):
        kind = "trip"
    elif intent in ("OnDutyPharmacyIntent",) or "φαρμακ" in low:
        kind = "pharmacy"
    elif intent in ("HospitalIntent",) or "νοσοκομ" in low:
        kind = "hospital"
    elif intent in ("ContactInfoIntent",) or "τηλέφων" in low:
        kind = "contact"
    pack = EMOJI_PACK.get(kind, EMOJI_PACK["generic"])
    if not re.match(r"^[\W_]{1,3}", t):
        if kind == "trip":
            t = f"{random.choice(['Πάμε!', 'Έτοιμοι;', 'ΟΚ!'])} {pack[0]} " + t
        elif kind == "pharmacy":
            t = f"{random.choice(['Βρήκα!', 'Έχουμε νέα!'])} {pack[0]} " + t
        elif kind in ("hospital", "contact"):
            t = f"{pack[0]} " + t
        else:
            t = f"{EMOJI_PACK['generic'][0]} " + t
    if kind == "trip" and ("διόδι" in low or "εκτίμηση" in low):
        t += "\n" + random.choice(["Θες να το κανονίσουμε; 🚖", "Να σου κάνω και επιστροφή; 🔁"])
    emoji_count = len(re.findall(r"[\U0001F300-\U0001FAFF]", t))
    while emoji_count > 4:
        t = re.sub(r"[\U0001F300-\U0001FAFF](?!.*[\U0001F300-\U0001FAFF])", "", t)
        emoji_count -= 1
    return t


# ──────────────────────────────────────────────────────────────────────────────
# Sticky intents

INTENT_TRIP = "TripCostIntent"
INTENT_PHARMACY = "OnDutyPharmacyIntent"
INTENT_HOSPITAL = "HospitalIntent"
INTENT_INFO = "PatrasLlmAnswersIntent"
INTENT_SERVICES = "ServicesAndToursIntent"

FOLLOWUP_BUDGET_DEFAULT = 3

# 🔧 ΤΡΟΠΟΠΟΙΗΘΗΚΕ: αφαιρέθηκε η «αποσκευ(ές|ες)» από TRIP triggers, προστέθηκαν «κοστίζουν/κοστιζουν»
TRIGGERS = {
    INTENT_TRIP: [
        r"\bδιαδρομ",
        r"\b(κοστ(ίζει|ιζ)|κοστιζει|κόστος|κοστος)\b",
        r"\b(κοστίζουν|κοστιζουν)\b",   # νέο
        r"\b(ταρ(ί)?φα)\b",
        r"\bστοιχ(ίζει|ιζ|ιζει)\b",
        r"\b(πόσο\s+πάει|πόσο\s+κάνει)\b",
        r"\bαπ[όο].+\b(μέχρι|προς|για)\b",
        r"\b(έως|εως|μέχρι|απ[όο])\b.*\b(διαδρομ|πάω|πάμε|ταξ|κοστος|κόστος|ταρ(ί)?φα|στοιχ)\b",
        r"\bεπιστροφ(ή|η)\b",
        # r"\bαποσκευ(ές|ες)\b",        # αφαιρέθηκε
        r"πόσα\s+χιλιόμετρα",
    ],
    INTENT_HOSPITAL: [
        r"νοσοκομ",
        r"εφημερ.*νοσο",
    ],
    INTENT_PHARMACY: [
        r"φαρμακ",
        r"(?<!νοσο)\bεφημερ",
    ],
    INTENT_INFO: [
        r"ξενοδοχ",
        r"παραλι",
        r"\bκαφε\b|\bcafe\b|\bκαφες\b",
        r"φαγητ|εστιατ",
        r"μουσει|μπανι",
        r"τροχαι",
        r"δημοτικ",
        r"ωραρια",
        r"τηλεφων(?!ο κρατηση?ς? ταξι)",
    ],
    INTENT_SERVICES: [
        r"εκδρομ",
        r"πακετ(α|ο)",
        r"\btour(s)?\b",
        r"vip",
        r"τουρισ",
        r"ολυμπ",
        r"δελφ",
        r"ναυπακ",
        r"γαλαξ",
        r"τι\s+περιλαμ",
        r"δεν\s+περιλαμ",
        r"υπηρεσ(ι|ιες|ίες|ια|ιων|εις)|\bservices?\b|\bservice\b",
        r"παιδι",
        r"σχολ(ει|ειο)",
        r"δεμα|δέμα|πακετ[οά]|courier",
        r"night\s*taxi|νυχτεριν(ο|ή)\s*ταξι",
        r"\bσχολ(είο|ειο)\b",
        r"\bραντεβ(ού|ου)\b",
        r"\bκρατ(ηση|ήση)\b",
        r"\bbooking\b",
    ],
}


# --- Tours renderer ---

def _brand_info():
    from constants import BRAND_INFO

    return {
        "phone": os.getenv("TAXI_EXPRESS_PHONE", BRAND_INFO.get("phone", "2610 450000")),
        "booking": os.getenv("TAXI_BOOKING_URL", BRAND_INFO.get("booking_url", "")),
        "site": os.getenv("TAXI_SITE_URL", BRAND_INFO.get("site_url", "https://taxipatras.com")),
        "email": os.getenv("TAXI_EMAIL", BRAND_INFO.get("email", "")),
    }


def _fmt_price(v) -> str:
    try:
        f = float(v)
        return f"{int(f)}€" if f.is_integer() else f"{f:.2f}€"
    except Exception:
        return f"{v}€" if v not in (None, "", "—") else "—"


def _fmt_duration_h(dur) -> str:
    if dur in (None, "", "—"):
        return "—"
    try:
        f = float(dur)
        return f"{int(f)}h" if f.is_integer() else f"{f}h"
    except Exception:
        s = str(dur).strip().lower()
        return s if s.endswith("h") else f"{s}h"


def render_tour_card(pkg: dict) -> str:
    title = (pkg.get("title") or "Εκδρομή").strip()
    price = _fmt_price(pkg.get("price_from"))
    dur = _fmt_duration_h(pkg.get("duration_hours") or pkg.get("duration_h") or "—")

    stops_list = pkg.get("stops") or []
    stops = " → ".join([s for s in stops_list if s][:6])

    includes = ", ".join((pkg.get("includes") or [])[:6]) or "Μεταφορά"
    excludes = ", ".join((pkg.get("excludes") or [])[:6]) or "—"

    pickup = pkg.get("pickup") or "Πάτρα"
    pax = pkg.get("passengers_included") or "έως 4 άτομα"

    brand = _brand_info()
    book = (pkg.get("book_url") or brand["booking"] or "").strip()

    lines = [
        f"🎒 {title}",
        f"💶 Τιμή: από {price}  |  ⏱️ Διάρκεια: ~{dur}" if dur != "—" else f"💶 Τιμή: από {price}",
        f"📍 Στάσεις: {stops}" if stops else "",
        f"✅ Περιλαμβάνει: {includes}",
        f"❌ Δεν περιλαμβάνει: {excludes}" if excludes and excludes != "—" else "",
        f"🚐 Παραλαβή: {pickup}  |  👥 {pax}",
    ]
    if book:
        lines.append(f"🧾 Κράτηση: {book}")
    return "\n".join([ln for ln in lines if ln.strip()])


def render_all_tours(packages: List[dict]) -> str:
    brand = _brand_info()
    pkgs = packages or []
    if not pkgs:
        return "Δεν βρήκα διαθέσιμες εκδρομές αυτή τη στιγμή."
    cards = [render_tour_card(p) for p in pkgs]
    footer = f"\nΚλείσιμο/Πληροφορίες: ☎️ {brand['phone']}"
    if brand["booking"]:
        footer += f" | 🧾 Booking: {brand['booking']}"
    return "\n\n".join(cards) + footer


def _nrm(s: str) -> str:
    s = _u_norm("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if ord(ch) < 0x0300)
    return re.sub(r"\s+", " ", s).strip()


def _tok(s: str) -> list[str]:
    s = _u_norm("NFKD", s or "")
    s = "".join(ch for ch in s if ch.isalnum() or ch.isspace())
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return [w for w in s.split(" ") if len(w) >= 3]


def _find_tour_by_query(q: str) -> Optional[dict]:
    from constants import TOUR_PACKAGES

    q_tokens = set(_tok(q))
    if not q_tokens:
        return None
    best = None
    best_overlap = 0
    for p in (TOUR_PACKAGES or []):
        title_tokens = set(_tok(p.get("title", "")))
        code_tokens = set(_tok(p.get("code", "")))
        stop_tokens = set(_tok(" ".join(p.get("stops", [])[:6])))
        cand_tokens = title_tokens | code_tokens | stop_tokens
        overlap = len(q_tokens & cand_tokens)
        if overlap > best_overlap:
            best, best_overlap = p, overlap
    return best if best_overlap >= 1 else None


def services_reply(query: str, st) -> str:
    from constants import SERVICES, TOUR_PACKAGES, BRAND_INFO

    phone = os.getenv("TAXI_EXPRESS_PHONE", BRAND_INFO.get("phone", "2610 450000"))
    booking = os.getenv("TAXI_BOOKING_URL", BRAND_INFO.get("booking_url", ""))
    qn = _nrm(query or "")
    
    if re.search(r"night\s*taxi|νυχτεριν(ο|η)\s*ταξι|νυχτα\s*ταξι", qn):
        from constants import TAXI_TARIFF as _TT
        def _tf(k, d):
            try:
                return float(_TT.get(k, d))
            except Exception:
                return d
        night_pct = int(_tf("night_multiplier", 1.0) * 100 - 100) if _tf("night_multiplier", 1.0) > 1 else 0
        wait_rate = int(_tf("wait_rate_per_hour", 18.0))
        lines = [
            "**Night Taxi**: νυχτερινές διαδρομές (00:00–05:00).",
            f"Επιβάρυνση: +{night_pct}% στα νυχτερινά (όπου ισχύει)." if night_pct else "",
            f"Αναμονή: ~{wait_rate}€/ώρα.",
            "Πληρωμή: Μετρητά/Κάρτα, προκράτηση διαθέσιμη.",
            f"☎️ {phone}" + (f" | 🧾 Booking: {booking}" if booking else ""),
        ]
        return "\n".join([l for l in lines if l])

    # NEW: Express courier definition
    if re.search(r"express\s*courier|δεμα|πακετ[οά]", qn):
        lines = [
            "**Express Courier**: ίδια μέρα παράδοση εγγράφων/δεμάτων με αυτοκίνητο.",
            "Παραλαβή από διεύθυνσή σου, παράδοση με υπογραφή & ενημέρωση.",
            "Χρέωση: ανά απόσταση/στάσεις/αναμονή.",
            f"☎️ {phone}" + (f" | 🧾 Booking: {booking}" if booking else ""),
        ]
        return "\n".join(lines)

    if re.search(r"(εκδρομ|tours?)", qn):
        keys = ("δελφ", "ολυμπ", "ναυπακ", "γαλαξ")
        pick = None
        for key in keys:
            if key in qn:
                pick = next(
                    (
                        p
                        for p in (TOUR_PACKAGES or [])
                        if re.search(key, _nrm(p.get("title", "") + " " + " ".join(p.get("stops") or [])))
                    ),
                    None,
                )
                if pick:
                    break
        if pick:
            st.slots["last_tour"] = pick.get("code") or pick.get("title")
            return render_tour_card(pick)
        return render_all_tours(TOUR_PACKAGES)

    tour = _find_tour_by_query(qn)
    if tour:
        st.slots["last_tour"] = tour.get("code") or tour.get("title")
        return render_tour_card(tour)

    if ("τι περιλαμ" in qn or "δεν περιλαμ" in qn) and st.slots.get("last_tour"):
        key = _nrm(str(st.slots["last_tour"]))
        pick = None
        for p in (TOUR_PACKAGES or []):
            if _nrm(p.get("code", "")) == key or _nrm(p.get("title", "")) == key:
                pick = p
                break
        if pick:
            if "τι περιλαμ" in qn:
                inc = ", ".join((pick.get("includes") or [])[:6]) or "Μεταφορά"
                return f"✅ Περιλαμβάνει: {inc}"
            else:
                exc = ", ".join((pick.get("excludes") or [])[:6]) or "—"
                return f"❌ Δεν περιλαμβάνει: {exc}"

    if re.search(r"(εκδρομ|tours?)", qn):
        return render_all_tours(TOUR_PACKAGES)

    lines = ["🧰 Υπηρεσίες:"]
    if isinstance(constants.SERVICES, list):
        for cat in constants.SERVICES:
            if isinstance(cat, dict) and cat.get("category") and cat.get("items"):
                lines.append(f"• {cat['category']}:")
                for it in cat["items"][:5]:
                    lines.append(f"  – {it}")
    if TOUR_PACKAGES:
        lines.append("")
        lines.append("🎒 Εκδρομές (σταθερή τιμή για 1–4 άτομα):")
        for p in TOUR_PACKAGES[:2]:
            lines.append(f"• {p.get('title','—')} — {p.get('price_from','—')}€ / ~{p.get('duration_hours','—')}h")
    lines.append("")
    c = f"Κλείσιμο/Πληροφορίες: ☎️ {phone}"
    if booking:
        c += f" | 🧾 Booking: {booking}"
    return "\n".join(lines)


def _match_triggers(text: str, intent: str) -> bool:
    for pat in TRIGGERS.get(intent, []):
        if re.search(pat, text, flags=re.IGNORECASE):
            return True
    return False

def _tf2(keys, d):
    for k in keys:
        try:
            if k in _TT:
                return float(_TT[k])
        except Exception:
            pass
    return d

_day_km = _tf2(["km_rate_city_or_day", "km_rate_zone1"], 0.90)
_night_km = _tf2(["km_rate_zone2_or_night"], max(_day_km, 1.25))
night_pct = int(round((_night_km / max(_day_km, 0.01) - 1.0) * 100)) if _night_km > _day_km else 0
wait_rate = int(_tf2(["wait_rate_per_hour", "waiting_per_hour"], 15.0))

# ──────────────────────────────────────────────────────────────────────────────
# Persisted memory store (Redis/Memory)

import json as _json
PERSIST_BACKEND = os.getenv("PERSIST_BACKEND", "memory")  # "redis" | "memory"
SESS_TTL_SECONDS = int(os.getenv("SESS_TTL_SECONDS", "2592000"))  # 30 μέρες


class BaseStore:
    def get(self, sid: str) -> Optional["SessionState"]:
        raise NotImplementedError

    def set(self, sid: str, st: "SessionState") -> None:
        raise NotImplementedError

    def delete(self, sid: str) -> None:
        raise NotImplementedError


class MemoryStore(BaseStore):
    def __init__(self):
        self._mem: Dict[str, dict] = {}
        self._exp: Dict[str, float] = {}
        self._lock = threading.RLock()

    def get(self, sid: str):
        with self._lock:
            exp = self._exp.get(sid)
            if exp and time.time() > exp:
                self._mem.pop(sid, None)
                self._exp.pop(sid, None)
                return None
            raw = self._mem.get(sid)
            return SessionState(**raw) if raw else None

    def set(self, sid: str, st: "SessionState"):
        with self._lock:
            self._mem[sid] = asdict(st)
            self._exp[sid] = time.time() + SESS_TTL_SECONDS

    def delete(self, sid: str):
        with self._lock:
            self._mem.pop(sid, None)
            self._exp.pop(sid, None)


class RedisStore(BaseStore):
    def __init__(self, url: str):
        import redis

        self.r = redis.Redis.from_url(url, decode_responses=True)
        self.prefix = "mrbooky:session:"

    def _key(self, sid: str) -> str:
        return f"{self.prefix}{sid}"

    def get(self, sid: str):
        s = self.r.get(self._key(sid))
        if not s:
            return None
        try:
            data = _json.loads(s)
            return SessionState(**data)
        except Exception:
            return None

    def set(self, sid: str, st: "SessionState"):
        payload = _json.dumps(asdict(st), ensure_ascii=False)
        self.r.set(self._key(sid), payload, ex=SESS_TTL_SECONDS)

    def delete(self, sid: str):
        self.r.delete(self._key(sid))


def make_store() -> BaseStore:
    if PERSIST_BACKEND.lower() == "redis":
        url = os.getenv("REDIS_URL", "")
        if not url:
            logger.warning("PERSIST_BACKEND=redis αλλά λείπει REDIS_URL – επιστρέφω MemoryStore")
            return MemoryStore()
        return RedisStore(url)
    return MemoryStore()


STORE: BaseStore = make_store()

# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class SessionState:
    intent: Optional[str] = None
    slots: Dict[str, Any] = field(default_factory=dict)
    budget: int = field(default=FOLLOWUP_BUDGET_DEFAULT)
    # 🔹 ΝΕΑ πεδία για router/booking/context
    last_offered: Optional[str] = None
    pending_trip: Dict[str, Any] = field(default_factory=dict)
    context_turns: List[str] = field(default_factory=list)
    booking_slots: Dict[str, Any] = field(default_factory=dict)


def _get_state(sid: str) -> "SessionState":
    st = STORE.get(sid)
    if not st:
        st = SessionState()
        STORE.set(sid, st)
    return st


def _save_state(sid: str, st: "SessionState"):
    STORE.set(sid, st)


def _clear_state(sid: str):
    STORE.delete(sid)


def _dec_budget(sid: str):
    st = _get_state(sid)
    st.budget -= 1
    if st.budget <= 0:
        _clear_state(sid)
    else:
        _save_state(sid, st)

# 🔹 helper για context buffer
def _push_context(sid: str, user_text: str, reply_text: str):
    st = _get_state(sid)
    st.context_turns.append(f"U: {user_text}")
    st.context_turns.append(f"A: {reply_text}")
    st.context_turns[:] = st.context_turns[-10:]
    _save_state(sid, st)


# --- Topic drift heuristics ---
DRIFT_SWITCH_MIN_HITS = int(os.getenv("DRIFT_SWITCH_MIN_HITS", "1"))
RESET_ON_NO_MATCH = os.getenv("RESET_ON_NO_MATCH", "1") == "1"


def _intent_trigger_hits(intent: str, text: str) -> int:
    pats = TRIGGERS.get(intent, [])
    return sum(1 for p in pats if re.search(p, text, flags=re.IGNORECASE))


def _best_intent_from_triggers(text: str, exclude: str | None = None):
    intents_order = (INTENT_TRIP, INTENT_HOSPITAL, INTENT_PHARMACY, INTENT_SERVICES, INTENT_INFO)
    best_intent, best_hits = None, 0
    for it in intents_order:
        if exclude and it == exclude:
            continue
        hits = _intent_trigger_hits(it, text)
        if hits > best_hits:
            best_intent, best_hits = it, hits
    return best_intent, best_hits


def _decide_intent(sid: str, text: str, predicted_intent: Optional[str], score: float) -> str:
    t = (text or "").lower()
    st = _get_state(sid)

    if is_cancel_message(t):
        _clear_state(sid)
        return ""

    # Αν υπάρχει ήδη intent, κάνε sticky/ελεγχόμενα switch μόνο με triggers
    if st.intent:
        cur_hits = _intent_trigger_hits(st.intent, t)
        cand_intent, cand_hits = _best_intent_from_triggers(t, exclude=None)

        if cur_hits == 0 and cand_intent and cand_intent != st.intent and cand_hits >= DRIFT_SWITCH_MIN_HITS:
            new_st = SessionState(intent=cand_intent)
            _save_state(sid, new_st)
            return cand_intent

        if cur_hits == 0 and (not cand_intent or cand_hits == 0):
            if RESET_ON_NO_MATCH:
                if CONFIRM_RE.search(t):
                    return st.intent
                _clear_state(sid)
                return ""

        missing = _missing_slots(st.intent, text, st)
        if missing:
            if _match_triggers(t, INTENT_TRIP):
                new_st = SessionState(intent=INTENT_TRIP)
                _save_state(sid, new_st)
                return INTENT_TRIP
            for intent in (INTENT_TRIP, INTENT_HOSPITAL, INTENT_PHARMACY, INTENT_SERVICES, INTENT_INFO):
                if intent != st.intent and _match_triggers(t, intent):
                    new_st = SessionState(intent=intent)
                    _save_state(sid, new_st)
                    return intent
            return st.intent

        for intent in (INTENT_TRIP, INTENT_HOSPITAL, INTENT_PHARMACY, INTENT_SERVICES, INTENT_INFO):
            if intent != st.intent and _match_triggers(t, intent):
                new_st = SessionState(intent=intent)
                _save_state(sid, new_st)
                return intent
        return st.intent

    # ΔΕΝ έχουμε intent: δεν κάνουμε auto-PHARMACY σε σκέτη περιοχή.
    for intent in (INTENT_TRIP, INTENT_HOSPITAL, INTENT_PHARMACY, INTENT_SERVICES, INTENT_INFO):
        if _match_triggers(t, intent):
            st_new = SessionState(intent=intent)
            _save_state(sid, st_new)
            return intent

    if predicted_intent in (INTENT_TRIP, INTENT_HOSPITAL, INTENT_PHARMACY, INTENT_SERVICES, INTENT_INFO) and score >= 0.70:
        st_new = SessionState(intent=predicted_intent)
        _save_state(sid, st_new)
        return predicted_intent

    return ""

def _two_word_cities_to_trip(text: str) -> Optional[str]:
    """
    Μετατρέπει input τύπου 'Πάτρα Πρέβεζα' σε 'από Πάτρα μέχρι Πρέβεζα'.
    Αποφεύγει λάθη όταν υπάρχουν λέξεις φαρμακείου/νοσοκομείου.
    """
    s = (text or "").strip()
    s = re.sub(r"[;;?!…]+$", "", s)
    if not s or "φαρμακ" in s.lower() or "εφημερ" in s.lower() or "νοσοκομ" in s.lower():
        return None
    tokens = re.split(r"\s+", s)
    if len(tokens) == 2 and all(len(t) >= 3 for t in tokens):
        return f"από {tokens[0]} μέχρι {tokens[1]}"
    return None

# ──────────────────────────────────────────────────────────────────────────────
# Helper: run tool with timeout
async def _run_tool_with_timeout(*, tool_input: str, ctx: dict):
    """Run a tool either via Agents SDK (if available) or directly by dispatching to our local functions."""
    if HAS_AGENTS_SDK:
        return await asyncio.wait_for(
            Runner.run(chat_agent, input=tool_input, context=ctx),
            timeout=getattr(settings, "TOOL_TIMEOUT_SEC", 25),
        )
    # Fallback: direct dispatch
    desired = (ctx or {}).get("desired_tool") or "ask_llm"
    # Map tool names to local callables (already imported at module scope)
    _registry = {
        "ask_llm": ask_llm,
        "trip_quote_nlp": trip_quote_nlp,
        "trip_estimate": trip_estimate,
        "pharmacy_lookup": pharmacy_lookup,
        "pharmacy_lookup_nlp": pharmacy_lookup_nlp,
        "hospital_duty": hospital_duty,
        "patras_info": patras_info,
        "taxi_contact": taxi_contact,
        "trendy_phrase": trendy_phrase,
    }
    fn = _registry.get(desired) or _registry.get("ask_llm")
    try:
        if fn is ask_llm:
            return fn(_RunCtx(context=ctx), tool_input)
        else:
            return fn(tool_input)
    except Exception:
        logger.exception("Direct tool dispatch failed (fallback)")
        return UI_TEXT.get("generic_error", "❌ Κάτι πήγε στραβά με το εργαλείο.")


# ──────────────────────────────────────────────────────────────────────────────
@app.post("/chat")
async def chat_endpoint(
    body: ChatRequest,
    request: Request,
):
    try:
        if not body.message:
            return {"reply": "Στείλε μου ένα μήνυμα 🙂"}
        if len(body.message) > getattr(settings, "MAX_MESSAGE_CHARS", 2000):
            return JSONResponse(status_code=413, content={"error": "Μήνυμα πολύ μεγάλο"})

        sid = body.session_id or body.user_id or "default"
        text = (body.message or "").strip()
        t_norm = text.lower()
        st = _get_state(sid)

        # 🔹 init νέα πεδία router/booking/context
        init_session_state(st)

        # Hard override για επικοινωνία/app
        if is_contact_intent(t_norm):
            reply = enrich_reply(_contact_reply(), intent="ContactInfoIntent")
            _push_context(sid, text, reply)
            return {"reply": reply}

        # 🔹 Router/Booking πρώτος έλεγχος ΠΡΙΝ από τα παλιά quick-confirm/regex
        handled = maybe_handle_followup_or_booking(st, text)
        if handled is not None:
            reply = handled["reply"]
            reply = enrich_reply(reply)  # απαλό styling
            _save_state(sid, st)        # ⭐ ΝΕΟ: αποθήκευσε τις αλλαγές του router (BookingIntent, slots κ.λπ.)
            _push_context(sid, text, reply)
            return {"reply": reply}


        # ✅ ΠΑΛΙΟ Quick path: επιβεβαίωση «ναι/σωστά/ok» επανατρέχει την τελευταία εκτίμηση ταξιδιού
        #    ΜΟΝΟ αν δεν υπάρχει ενεργή προσφορά από τον router (booking/quote/baggage)
        if (
            CONFIRM_RE.search(t_norm)
            and st.intent == INTENT_TRIP
            and st.slots.get("last_trip_query")
            and st.last_offered not in {"booking_confirm", "trip_quote", "baggage_cost_info"}
        ):
            tool_input = _apply_location_aliases(st.slots["last_trip_query"])
            run_context = {
                "user_id": body.user_id,
                "context_text": body.context or "",
                "history": body.history or [],
                "system_prompt": SYSTEM_PROMPT,
                "desired_tool": "trip_quote_nlp",
            }
            result = await _run_tool_with_timeout(tool_input=tool_input, ctx=run_context)
            reply_raw = result.final_output or "❌ Κάτι πήγε στραβά, να το ξαναπροσπαθήσω;"
            reply, map_url = strip_map_link(reply_raw)
            _dec_budget(sid)
            reply = inject_trendy_phrase(reply, st=_get_state(sid), intent=INTENT_TRIP, success=True)
            reply = enrich_reply(reply, intent=INTENT_TRIP)
            _push_context(sid, text, reply)
            resp = {"reply": reply}
            if map_url:
                resp["map_url"] = map_url
            return resp

        # 1) Intent detect (sticky)
        predicted_intent, score = (None, 0.0)
        if INTENT_CLF:
            try:
                out = predict_intent(text)
                if isinstance(out, tuple) and len(out) == 2:
                    predicted_intent, score = out
                elif isinstance(out, str):
                    predicted_intent, score = out, 1.0
            except Exception:
                logger.exception("Intent classification failed")

        intent = _decide_intent(sid, text, predicted_intent, score)

        ui = getattr(constants, "UI_TEXT", {}) or {}

        if intent == "" and is_cancel_message(t_norm):
            reply = enrich_reply("ΟΚ, το αφήνουμε εδώ 🙂 Πες μου τι άλλο θες να κανονίσουμε!")
            _push_context(sid, text, reply)
            return {"reply": reply}

        # Αν δεν αποφασίστηκε intent: πιάσε το μοτίβο “Πάτρα Ιωάννινα” ως TRIP
        if not intent:
            tw = _two_word_cities_to_trip(text)
            if tw:
                st.intent = INTENT_TRIP
                st.slots["last_trip_query"] = tw
                _save_state(sid, st)
                intent = INTENT_TRIP
                text = tw  # normalize για το εργαλείο

        # 2) Intent-specific

        # --- PHARMACY ---
        if intent == INTENT_PHARMACY:
            st = _get_state(sid)
            area = detect_area_for_pharmacy(text) or st.slots.get("area")
            ui = getattr(constants, "UI_TEXT", {}) or {}

            if not area:
                st.slots["area"] = None
                _save_state(sid, st)
                ask = ui.get(
                    "ask_pharmacy_area",
                    "Για ποια περιοχή να ψάξω εφημερεύον φαρμακείο; π.χ. Πάτρα, Ρίο, Βραχναίικα, Μεσσάτιδα/Οβρυά, Παραλία Πατρών. 😊",
                )
                reply = enrich_reply(ask, intent=intent)
                _push_context(sid, text, reply)
                return {"reply": reply}

            try:
                client = PharmacyClient()
                resp = client.get_on_duty(area=area)  # μόνο /pharmacy πλέον
                items = (resp or {}).get("pharmacies", [])

                if not items:
                    none_msg = ui.get(
                        "pharmacy_none_for_area",
                        "❌ Δεν βρέθηκαν εφημερεύοντα για {area}. Θες να δοκιμάσουμε άλλη περιοχή?"
                    ).format(area=area)
                    reply = enrich_reply(none_msg, intent=intent)
                    _push_context(sid, text, reply)
                    return {"reply": reply}

                # --- ΑΠΛΟ SESSION CACHE ΣΕ ΕΠΙΠΕΔΟ TEXT ---
                cached: dict = st.slots.get("cached_pharmacy", {})
                if isinstance(cached, dict) and area in cached:
                    logger.info(f"✅ Returning cached pharmacy info for {area}")
                    pharm_text = cached[area]
                else:
                    logger.info(f"🔄 No cache hit for {area} — rendering from API items")
                    pharm_text = _render_pharmacies_text(items, area)
                    # cache μόνο αν δεν είναι error
                    if "Δεν βρέθηκαν" not in pharm_text:
                        cached[area] = pharm_text
                        st.slots["cached_pharmacy"] = cached

                st.slots["area"] = area
                _save_state(sid, st)
                _dec_budget(sid)

                # --- ΤΕΛΙΚΟ ΜΗΝΥΜΑ (χωρίς Runner.run/LLM) ---
                reply = f"**Περιοχή: {area}**\n{pharm_text}"
                reply = inject_trendy_phrase(reply, st=st, intent=intent, success=True)
                reply = enrich_reply(reply, intent=intent)
                _push_context(sid, text, reply)
                return {"reply": reply}

            except Exception:
                logger.exception("PharmacyClient call failed")
                generic = ui.get(
                    "generic_error",
                    "❌ Κάτι πήγε στραβά με την αναζήτηση. Θες να δοκιμάσουμε άλλη περιοχή;"
                )
                reply = enrich_reply(generic, intent=intent)
                _push_context(sid, text, reply)
                return {"reply": reply}


        # --- HOSPITAL ---
        if intent == INTENT_HOSPITAL:
            which_day = "σήμερα"
            if "αυρ" in t_norm or "αύρ" in t_norm or "tomorrow" in t_norm:
                which_day = "αύριο"
            st = _get_state(sid)
            st.slots["which_day"] = which_day
            _save_state(sid, st)

            try:
                run_context = {
                    "user_id": body.user_id,
                    "context_text": body.context or "",
                    "history": body.history or [],
                    "system_prompt": SYSTEM_PROMPT,
                    "desired_tool": "hospital_duty",
                }
                result = await _run_tool_with_timeout(tool_input=f"νοσοκομεία {which_day}", ctx=run_context)
                _dec_budget(sid)
                out = result.final_output or "❌ Δεν μπόρεσα να φέρω την εφημερία."
                out = inject_trendy_phrase(out, st=_get_state(sid), intent=intent, success=True)
                reply = enrich_reply(out, intent=intent)
                _push_context(sid, text, reply)
                return {"reply": reply}
            except Exception:
                logger.exception("Hospital intent failed")
                reply = enrich_reply("❌ Δεν κατάφερα να φέρω εφημερεύοντα νοσοκομεία.", intent=intent)
                _push_context(sid, text, reply)
                return {"reply": reply}

        # --- TRIP COST ---
        if intent == INTENT_TRIP:
            try:
                from tools import _extract_route_free_text
            except Exception:
                _extract_route_free_text = None

            st = _get_state(sid)
            use_memory = False
            km_msg = _km_query_to_trip_message(text)
            norm_msg = None
            price_msg = _price_query_to_trip_message(text, st)
            partial_msg = _partial_to_full_trip(text, st)
            twoword_msg = _two_word_cities_to_trip(text)

            if _extract_route_free_text and not km_msg and not partial_msg and not price_msg and not twoword_msg:
                o, d = _extract_route_free_text(text)
                if o and d:
                    st.slots["last_origin"], st.slots["last_dest"] = o, d
                    _save_state(sid, st)
                    norm_msg = f"από {o} μέχρι {d} {text}"
                elif not d:
                    if any(x in t_norm for x in ["ίδια", "ιδια", "διπλ", "νυχτ", "βράδ", "βραδ", "επιστροφ"]):
                        if st.slots.get("last_origin") and st.slots.get("last_dest"):
                            use_memory = True

            tool_input = (
                km_msg
                or norm_msg
                or partial_msg
                or twoword_msg
                or price_msg
                or (
                    f"από {st.slots['last_origin']} μέχρι {st.slots['last_dest']} {text}" if use_memory else text
                )
            )
            tool_input = _apply_location_aliases(tool_input)
            st.slots["last_trip_query"] = tool_input
            _save_state(sid, st)

            run_context = {
                "user_id": body.user_id,
                "context_text": body.context or "",
                "history": body.history or [],
                "system_prompt": SYSTEM_PROMPT,
                "desired_tool": "trip_quote_nlp",
            }
            result = await _run_tool_with_timeout(tool_input=tool_input, ctx=run_context)
            reply_raw = result.final_output or "❌ Κάτι πήγε στραβά, να το ξαναπροσπαθήσω;"

            reply, map_url = strip_map_link(reply_raw)
            _dec_budget(sid)
            reply = inject_trendy_phrase(reply, st=_get_state(sid), intent=intent, success=True)
            reply = enrich_reply(reply, intent=intent)
            _push_context(sid, text, reply)
            resp = {"reply": reply}
            if map_url:
                resp["map_url"] = map_url
            return resp

        # --- SERVICES / TOURS ---
        if intent == INTENT_SERVICES:
            _dec_budget(sid)
            st = _get_state(sid)

            # (FIX) χρησιμοποιούμε 'and' αντί για ελληνικό 'και'
            if re.search(r"(τι\s+περιλαμ|δεν\s+περιλαμ)", t_norm) and st.slots.get("last_tour"):
                key = _nrm(str(st.slots["last_tour"]))
                pick = next(
                    (
                        p
                        for p in (TOUR_PACKAGES or [])
                        if _nrm(p.get("code", "")) == key or _nrm(p.get("title", "")) == key
                    ),
                    None,
                )
                if pick:
                    if re.search(r"τι\s+περιλαμ", t_norm):
                        inc = ", ".join((pick.get("includes") or [])[:6]) or "Μεταφορά"
                        msg = enrich_reply(f"✅ Περιλαμβάνει: {inc}", intent=intent)
                        _save_state(sid, st)
                        _push_context(sid, text, msg)
                        return {"reply": msg}
                    else:
                        exc = ", ".join((pick.get("excludes") or [])[:6]) or "—"
                        msg = enrich_reply(f"❌ Δεν περιλαμβάνει: {exc}", intent=intent)
                        _save_state(sid, st)
                        _push_context(sid, text, msg)
                        return {"reply": msg}

            if re.search(r"(εκδρομ|tours?)", t_norm):
                msg = render_all_tours(TOUR_PACKAGES)
                msg = enrich_reply(msg, intent=intent)
                try:
                    msg = inject_trendy_phrase(msg, st=st, intent=intent, success=True)
                except Exception:
                    pass
                _save_state(sid, st)
                _push_context(sid, text, msg)
                return {"reply": msg}

            if re.search(r"(δελφ|ολυμπ|ναυπακ|γαλαξ)", _nrm(text)):
                pick = _find_tour_by_query(_nrm(text)) or next(
                    (
                        p
                        for p in (TOUR_PACKAGES or [])
                        if re.search(r"(δελφ|ολυμπ|ναυπακ|γαλαξ)", _nrm(p.get('title','') + ' ' + ' '.join(p.get('stops') or [])))
                    ),
                    None,
                )
                if pick:
                    st.slots["last_tour"] = pick.get("code") or pick.get("title")
                    _save_state(sid, st)
                    msg = render_tour_card(pick)
                    msg = enrich_reply(msg, intent=intent)
                    try:
                        msg = inject_trendy_phrase(msg, st=st, intent=intent, success=True)
                    except Exception:
                        pass
                    _push_context(sid, text, msg)
                    return {"reply": msg}

            msg = services_reply(text, st)
            _save_state(sid, st)
            msg = enrich_reply(msg, intent=intent)
            try:
                msg = inject_trendy_phrase(msg, st=st, intent=intent, success=True)
            except Exception:
                pass
            _push_context(sid, text, msg)
            return {"reply": msg}

        # --- INFO / LLM Πάτρας ---
        if intent == INTENT_INFO:
            run_context = {
                "user_id": body.user_id,
                "context_text": body.context or "",
                "history": body.history or [],
                "system_prompt": SYSTEM_PROMPT,
                "desired_tool": "patras_info",
            }
            result = await _run_tool_with_timeout(tool_input=text, ctx=run_context)
            _dec_budget(sid)
            out = result.final_output or "Δεν βρήκα κάτι σχετικό, θες να το ψάξω αλλιώς?"
            out = inject_trendy_phrase(out, st=_get_state(sid), intent=intent, success=True)
            reply = enrich_reply(out, intent=intent)
            _push_context(sid, text, reply)
            return {"reply": reply}

        # 3) Γενικό fallback
        desired_tool = None
        if predicted_intent in INTENT_TOOL_MAP and score >= 0.70:
            desired_tool = INTENT_TOOL_MAP[predicted_intent]
        elif is_contact_intent(text):
            desired_tool = "taxi_contact"
        elif is_trip_quote(text):
            desired_tool = "trip_quote_nlp"
        elif re.search(r"υπηρεσ|εκδρομ|tour|πακετ", t_norm):
            desired_tool = "__internal_services__"

        # Αν μοιάζει με «δύο πόλεις» και δεν έχει triggers για pharmacy/hospital → στείλ’το ως trip
        if desired_tool is None:
            tw = _two_word_cities_to_trip(text)
            if tw and not is_pharmacy_message(text) and not is_hospital_message(text):
                desired_tool = "trip_quote_nlp"
                text = tw  # normalize

        if desired_tool == "taxi_contact":
            reply = enrich_reply(_contact_reply(), intent="ContactInfoIntent")
            _push_context(sid, text, reply)
            return {"reply": reply}
        if desired_tool == "__internal_services__":
            _dec_budget(sid)
            st = _get_state(sid)
            msg = services_reply(text, st)
            _save_state(sid, st)
            msg = enrich_reply(msg, intent=INTENT_SERVICES)
            try:
                msg = inject_trendy_phrase(msg, st=st, intent=INTENT_SERVICES, success=True)
            except Exception:
                pass
            _push_context(sid, text, msg)
            return {"reply": msg}

        if desired_tool == "trip_quote_nlp":
            try:
                from tools import _extract_route_free_text
            except Exception:
                _extract_route_free_text = None
            st = _get_state(sid)
            km_msg = _km_query_to_trip_message(text)
            price_msg = _price_query_to_trip_message(text, st)
            norm_msg = None
            partial_msg = _partial_to_full_trip(text, st)
            twoword_msg = _two_word_cities_to_trip(text)
            if _extract_route_free_text and not km_msg and not twoword_msg:
                o, d = _extract_route_free_text(text)
                if o and d:
                    st.slots["last_origin"], st.slots["last_dest"] = o, d
                    _save_state(sid, st)
                    norm_msg = f"από {o} μέχρι {d} {text}"
            tool_input = km_msg or norm_msg or partial_msg or twoword_msg or price_msg or text
            tool_input = _apply_location_aliases(tool_input)
            st.slots["last_trip_query"] = tool_input
            _save_state(sid, st)
            run_context = {
                "user_id": body.user_id,
                "context_text": body.context or "",
                "history": body.history or [],
                "system_prompt": SYSTEM_PROMPT,
                "desired_tool": "trip_quote_nlp",
            }
            result = await _run_tool_with_timeout(tool_input=tool_input, ctx=run_context)
            reply_raw = result.final_output or ""

            reply, map_url = strip_map_link(reply_raw)
            reply = inject_trendy_phrase(reply, st=_get_state(sid), intent=INTENT_TRIP, success=True)
            reply = enrich_reply(reply, intent=INTENT_TRIP)
            _push_context(sid, text, reply)
            resp = {"reply": reply}
            if map_url:
                resp["map_url"] = map_url
            return resp

        # Τελικό agent fallback
        run_context = {
            "user_id": body.user_id,
            "context_text": body.context or "",
            "history": body.history or [],
            "system_prompt": SYSTEM_PROMPT,
            "predicted_intent": predicted_intent,
            "desired_tool": desired_tool,
        }
        result = await _run_tool_with_timeout(tool_input=text, ctx=run_context)
        reply_raw = result.final_output or ""
        reply, map_url = strip_map_link(reply_raw)
        reply = inject_trendy_phrase(reply, st=_get_state(sid), intent=intent or "", success=True)
        reply = enrich_reply(reply)
        _push_context(sid, text, reply)
        resp = {"reply": reply}
        if map_url:
            resp["map_url"] = map_url
        return resp

    except asyncio.TimeoutError:
        origin = request.headers.get("origin", "")
        return JSONResponse(status_code=504, content={"error": "Upstream timeout"}, headers=_cors_headers(origin))
    except Exception:
        logger.exception("Agent execution failed")
        origin = request.headers.get("origin", "")
        return JSONResponse(status_code=500, content={"error": "Agent execution error"}, headers=_cors_headers(origin))


# ──────────────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok"}
