# main.py
from __future__ import annotations
import os
import json
import logging
import re
import random
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agents import Agent, Runner, function_tool
from config import Settings
from dataclasses import dataclass, field
import constants
from api_clients import PharmacyClient

# εργαλεία
from tools import (
    taxi_contact,
    trip_estimate,
    pharmacy_lookup,
    pharmacy_lookup_nlp,
    hospital_duty,
    patras_info,
    trip_quote_nlp,
    ask_llm,
    detect_area_for_pharmacy,
)

# ──────────────────────────────────────────────────────────────────────────────
# .env + settings
load_dotenv()
settings = Settings()

app = FastAPI()

ALLOWED_ORIGINS = {
    "https://storage.googleapis.com",
    # "https://mr.booky.taxipatras.com",
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=600,
)

def _cors_headers(origin: str) -> dict:
    if origin in ALLOWED_ORIGINS:
        return {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Max-Age": "600",
        }
    return {}

# ──────────────────────────────────────────────────────────────────────────────
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
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
]
TOOLS = [ensure_tool(t) for t in tool_candidates]
for t in TOOLS:
    logger.info("🔧 tool loaded: %s (%s)", getattr(t, "name", t), type(t))

chat_agent = Agent(
    name="customer_support_agent",
    instructions=(
        "Είσαι ένας ζεστός, χιουμοριστικός agent εξυπηρέτησης πελατών (Taxi Express Patras). "
        "Απάντα στα ελληνικά. "
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
    "PatrasLlmAnswersIntent": "patras_info",     # γενικά/εκδρομές/τοπικά
    "ServicesAndToursIntent": "patras_info",     # ρητά για εκδρομές
}

CONTACT_PAT = re.compile(
    r"(ταξι|taxi|radio\s*taxi|taxi\s*express|taxipatras|ραδιοταξι).*(τηλ|τηλέφων|επικοινων|booking|app|site|σελίδ)"
    r"|(?:(τηλ|τηλέφων).*?(ταξι|taxi|taxi\s*express|taxipatras|ραδιοταξι))",
    re.IGNORECASE
)
def is_contact_intent(text: str) -> bool:
    return bool(CONTACT_PAT.search(text or ""))


TIME_RANGE_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\s*[-–]\s*\d{1,2}[:.]\d{2}\b")

TRIP_PAT = re.compile(
    r"(πόσο|κοστίζει|τιμή|ταξί|ταξι|fare|cost).*(από).*(μέχρι|προς|για)",
    re.IGNORECASE | re.DOTALL
)

def is_trip_quote(text: str) -> bool:
    t = (text or "").lower()
    if TIME_RANGE_RE.search(t):
        return False  # μοιάζει με ωράριο, όχι διαδρομή
    # απαιτεί ρήμα/λέξη σχετική με μετακίνηση ή κόστος
    movement_kw = any(w in t for w in ["πάω", "πάμε", "μετάβαση", "διαδρομή", "route", "ταξ", "κοστίζει", "τιμή"])
    return bool(TRIP_PAT.search(t)) or (movement_kw and "από" in t and any(w in t for w in ["μέχρι","προς","για"]))

PHARMACY_RE = re.compile(r"\b(φαρμακ|εφημερ)\b", re.IGNORECASE)
HOSPITAL_RE = re.compile(r"\b(νοσοκομ|εφημερ[^\s]*\s*νοσοκομ)\b", re.IGNORECASE)

def is_pharmacy_message(text: str) -> bool:
    return bool(PHARMACY_RE.search(text or ""))

def is_hospital_message(text: str) -> bool:
    return bool(HOSPITAL_RE.search(text or ""))

# --- στο main.py, κοντά στα helpers ---

def _missing_slots(intent: str, text: str, st: "SessionState") -> list[str]:
    t = (text or "").lower()
    if intent == INTENT_PHARMACY:
        # area από τωρινό μήνυμα ή από state
        area = detect_area_for_pharmacy(text) or st.slots.get("area")
        return ["area"] if not area else []
    if intent == INTENT_TRIP:
        # χρειάζεται τουλάχιστον προορισμός
        try:
            from tools import _extract_route_free_text  # ήδη υπάρχει στα tools.py
        except Exception:
            return []
        o, d = _extract_route_free_text(text)
        return ["destination"] if not d else []
    return []

def _is_hard_override(intent: str) -> bool:
    # μόνο TRIP / HOSPITAL να μπορούν να «σπάσουν» sticky intent όταν λείπουν slots
    return intent in (INTENT_TRIP, INTENT_HOSPITAL)

# ──────────────────────────────────────────────────────────────────────────────
# Map link stripper -> UI βγάζει κουμπί
MAP_RE_MD  = re.compile(r"\[[^\]]*\]\((https?://www\.google\.com/maps/dir/\?[^)]+)\)")
MAP_RE_RAW = re.compile(r"(https?://www\.google\.com/maps/dir/\?[^ \t\n\r<>]+)")

def strip_map_link(text: str):
    if not text:
        return text, None
    m = MAP_RE_MD.search(text)
    if m:
        url = m.group(1)
        cleaned = MAP_RE_MD.sub("", text)
        return cleaned.strip(), url
    m = MAP_RE_RAW.search(text)
    if m:
        url = m.group(1)
        cleaned = MAP_RE_RAW.sub("", text)
        return cleaned.strip(), url
    return text, None

def _contact_reply() -> str:
    brand = getattr(constants, "BRAND_INFO", {}) or {}
    phone   = os.getenv("TAXI_EXPRESS_PHONE", brand.get("phone", "2610 450000"))
    site    = os.getenv("TAXI_SITE_URL", brand.get("site_url", "https://taxipatras.com"))
    booking = os.getenv("TAXI_BOOKING_URL", brand.get("booking_url", ""))
    appurl  = os.getenv("TAXI_APP_URL", brand.get("app_url", ""))

    lines = [
        f"📞 Τηλέφωνο: {phone}",
        f"🌐 Ιστότοπος: {site}",
    ]
    if booking:
        lines.append(f"🧾 Online κράτηση: {booking}")
    if appurl:
        lines.append(f"📱 Εφαρμογή: {appurl}")
    lines.append("🚖 Εναλλακτικά: Καλέστε μας στο 2610450000")
    return "\n".join(lines)

# ──────────────────────────────────────────────────────────────────────────────
# Emojis / εμπλουτισμός κειμένων
EMOJI_PACK = {
    "trip": ["🚕","🛣️","🕒","📍","💶"],
    "pharmacy": ["💊","🕘","📍","🧭"],
    "hospital": ["🏥","🚑","🩺"],
    "contact": ["☎️","🌐","🧾","📱"],
    "generic": ["✨","🙂","🙌"]
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
        elif kind in ("hospital","contact"):
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

INTENT_TRIP      = "TripCostIntent"
INTENT_PHARMACY  = "OnDutyPharmacyIntent"
INTENT_HOSPITAL  = "HospitalIntent"
INTENT_INFO      = "PatrasLlmAnswersIntent"
INTENT_SERVICES  = "ServicesAndToursIntent"

FOLLOWUP_BUDGET_DEFAULT = 3  # πόσα consecutive follow-ups μένουμε στο ίδιο intent
CANCEL_WORDS = {"άκυρο", "ακυρο", "cancel", "τέλος", "τελος", "σταμάτα", "σταματα"}

TRIGGERS = {
    INTENT_TRIP: [
        r"\bδιαδρομ",
        r"\bκοστ(ίζει|ιζ)\b",
        r"\bπόσο\s+πάει\b",
        r"\bαπ[όο].+\b(μέχρι|προς|για)\b",
    ],
    # Βάλε το hospital πριν το pharmacy ως “πιο δυνατό”
    INTENT_HOSPITAL: [
        r"νοσοκομ",
        r"εφημερ.*νοσο",
    ],
    INTENT_PHARMACY: [
        r"φαρμακ",
        r"(?<!νοσο)\bεφημερ",
    ],
    INTENT_INFO: [
        r"ξενοδοχ", r"παραλι", r"\bκαφε\b|\bcafe\b|\bκαφες\b", r"φαγητ|εστιατ", r"μουσει|μπανι",
        r"τροχαι", r"δημοτικ", r"ωραρια", r"τηλεφων(?!ο κρατηση?ς? ταξι)",
    ],
    INTENT_SERVICES: [
        r"εκδρομ", r"πακετ(α|ο)", r"tour", r"vip", r"τουρισ",
        r"παιδι", r"σχολ(ει|ειο)",           # Taxi School
        r"δεμα|δέμα|πακετ[οά]|courier",      # Courier
        r"night\s*taxi|νυχτεριν(ο|ή)\s*ταξι", # Night Taxi
    ],

}

def _format_services_reply(query: str = "") -> str:
    q = (query or "").lower()
    # Taxi School
    if re.search(r"παιδι|σχολ(ει|ειο)", q):
        return ("🧒 Taxi School: ασφαλής μεταφορά μαθητών από/προς σχολείο, σταθερές διαδρομές, ενημέρωση γονέων.\n"
                "Κλείσιμο/Πληροφορίες: ☎️ 2610 450000")
    # Courier / Δέματα
    if re.search(r"δεμα|δέμα|πακετ|courier", q):
        return ("📦 Express Courier: γρήγορη μεταφορά φακέλων/δεμάτων εντός/εκτός Πάτρας με απόδειξη παράδοσης.\n"
                "Κλείσιμο/Πληροφορίες: ☎️ 2610 450000")
    # Night Taxi
    if re.search(r"night\s*taxi|νυχτεριν(ο|ή)\s*ταξι", q):
        return ("🌙 Night Taxi: διαθέσιμο 24/7, ασφαλείς μετακινήσεις νύχτα.\n"
                "Κλείσιμο/Πληροφορίες: ☎️ 2610 450000")

    # Fallback: δείξε και υπηρεσίες και tours συνοπτικά
    from constants import SERVICES, TOUR_PACKAGES
    lines = ["🧰 Υπηρεσίες:", "• Μεταφορές αεροδρόμια/λιμάνια/ξενοδοχεία", "• Εταιρικές μετακινήσεις & events",
             "• Express courier", "• Night Taxi", "• Taxi School (μεταφορά παιδιών)", ""]
    if TOUR_PACKAGES:
        lines.append("🎒 Εκδρομές (1–4 άτομα):")
        for p in TOUR_PACKAGES[:2]:
            lines.append(f"• {p.get('title')} — {p.get('price_eur','—')}€ / ~{p.get('duration_h','—')}h")
    lines.append("")
    lines.append("Κλείσιμο/Πληροφορίες: ☎️ 2610 450000")
    return "\n".join(lines)


def _match_triggers(text: str, intent: str) -> bool:
    for pat in TRIGGERS.get(intent, []):
        if re.search(pat, text, flags=re.IGNORECASE):
            return True
    return False

@dataclass
class SessionState:
    intent: Optional[str] = None
    slots: Dict[str, Any] = field(default_factory=dict)  # π.χ. area, day, origin/dest
    budget: int = field(default=FOLLOWUP_BUDGET_DEFAULT)

SESSION: Dict[str, SessionState] = {}  # session_id -> state

def _get_state(sid: str) -> SessionState:
    if sid not in SESSION:
        SESSION[sid] = SessionState()
    return SESSION[sid]

def _clear_state(sid: str):
    if sid in SESSION:
        SESSION.pop(sid, None)

def _dec_budget(sid: str):
    st = _get_state(sid)
    st.budget -= 1
    if st.budget <= 0:
        _clear_state(sid)

def _decide_intent(sid: str, text: str, predicted_intent: Optional[str], score: float) -> str:
    """Μένουμε στο τρέχον intent και αλλάζουμε ΜΟΝΟ αν υπάρχουν strong triggers άλλου intent."""
    t = (text or "").lower()
    st = _get_state(sid)

    if any(w in t for w in CANCEL_WORDS):
        _clear_state(sid)
        return ""

    # --- Αν έχουμε ήδη ενεργό intent ---
    if st.intent:
        # 1) Αν το ενεργό intent είναι PHARMACY και το μήνυμα μοιάζει με περιοχή → μείνε PHARMACY
        try:
            area_guess = detect_area_for_pharmacy(text)
        except Exception:
            area_guess = None
        if st.intent == INTENT_PHARMACY and area_guess:
            # γέμισε slot και μείνε στο ίδιο intent
            st.slots["area"] = area_guess
            return st.intent

        # 2) Αν λείπουν slots στο ενεργό intent → ΜΗΝ αλλάζεις σε INFO/SERVICES
        missing = _missing_slots(st.intent, text, st)
        if missing:
            # επίτρεψε override μόνο σε "σκληρά" intents (TRIP/HOSPITAL)
            for intent in (INTENT_TRIP, INTENT_HOSPITAL, INTENT_PHARMACY, INTENT_SERVICES, INTENT_INFO):
                if intent != st.intent and _match_triggers(t, intent) and _is_hard_override(intent):
                    SESSION[sid] = SessionState(intent=intent)  # reset state/budget για νέο intent
                    return intent
            # αλλιώς μείνε στο ενεργό
            return st.intent

        # 3) Αν ΔΕΝ λείπουν slots, ισχύει η υπάρχουσα συμπεριφορά αλλαγής με triggers
        for intent in (INTENT_TRIP, INTENT_HOSPITAL, INTENT_PHARMACY, INTENT_SERVICES, INTENT_INFO):
            if intent != st.intent and _match_triggers(t, intent):
                SESSION[sid] = SessionState(intent=intent)
                return intent
        return st.intent

    # --- Δεν υπάρχει ενεργό intent: area-only heuristic για PHARMACY ---
    try:
        area_guess = detect_area_for_pharmacy(text)
    except Exception:
        area_guess = None
    if area_guess and len((text or "").split()) <= 3:
        SESSION[sid] = SessionState(intent=INTENT_PHARMACY)
        SESSION[sid].slots["area"] = area_guess
        return INTENT_PHARMACY

    # --- Νέο intent από triggers ---
    for intent in (INTENT_TRIP, INTENT_HOSPITAL, INTENT_PHARMACY, INTENT_SERVICES, INTENT_INFO):
        if _match_triggers(t, intent):
            SESSION[sid] = SessionState(intent=intent)
            return intent

    # --- Classifier fallback ---
    if predicted_intent in (INTENT_TRIP, INTENT_HOSPITAL, INTENT_PHARMACY, INTENT_SERVICES, INTENT_INFO) and score >= 0.80:
        SESSION[sid] = SessionState(intent=predicted_intent)
        return predicted_intent

    return ""


def _format_tours_reply() -> str:
    from constants import TOUR_PACKAGES
    brand = getattr(constants, "BRAND_INFO", {}) or {}
    phone   = os.getenv("TAXI_EXPRESS_PHONE", brand.get("phone", "2610 450000"))
    booking = os.getenv("TAXI_BOOKING_URL", brand.get("booking_url", ""))

    if not TOUR_PACKAGES:
        return f"Δεν βρήκα διαθέσιμα πακέτα αυτή τη στιγμή. Θες να σε καλέσουμε; 🙂\nΚλήση: {phone}" + (f" | Booking: {booking}" if booking else "")

    lines = ["🎒 Διαθέσιμες εκδρομές (σταθερή τιμή για 1–4 άτομα):"]
    for p in TOUR_PACKAGES:
        title = p.get("title","")
        price = p.get("price_eur","—")
        dur   = p.get("duration_h","—")
        highlights = ", ".join(p.get("highlights", [])[:3])
        lines.append(f"• {title} — {price}€ / ~{dur}h")
        if highlights:
            lines.append(f"  ↳ {highlights}")
    lines.append("")
    lines.append(f"Κλείσιμο/Πληροφορίες: ☎️ {phone}" + (f" | 🧾 Booking: {booking}" if booking else ""))
    return "\n".join(lines)

def _format_pharmacies(groups: List[Dict[str, str]]) -> str:
    if not groups:
        return "❌ Δεν βρέθηκαν εφημερεύοντα."
    buckets: Dict[str, List[Dict[str, str]]] = {}
    for p in groups:
        tr = (p.get("time_range") or "Ώρες μη διαθέσιμες").strip()
        buckets.setdefault(tr, []).append(p)

    def _start_minutes(s: str) -> int:
        m = re.search(r"(\d{1,2}):(\d{2})", s or "")
        return int(m.group(1)) * 60 + int(m.group(2)) if m else 10_000

    lines: List[str] = []
    for tr in sorted(buckets.keys(), key=_start_minutes):
        lines.append(f"**{tr}**")
        for p in buckets[tr]:
            name = (p.get("name") or "—").strip()
            addr = (p.get("address") or "—").strip()
            lines.append(f"{name} — {addr}")
        lines.append("")
    return "\n".join(lines).strip()

# ──────────────────────────────────────────────────────────────────────────────
@app.post("/chat")
async def chat_endpoint(body: ChatRequest, request: Request):
    try:
        sid = body.session_id or body.user_id or "default"
        text = (body.message or "").strip()
        t_norm = text.lower()

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

        if intent == "" and any(w in t_norm for w in CANCEL_WORDS):
            return {"reply": enrich_reply("ΟΚ, το αφήνουμε εδώ 🙂 Πες μου τι άλλο θες να κανονίσουμε!")}

        # 2) Intent-specific χειρισμός

        # --- PHARMACY ---
        if intent == INTENT_PHARMACY:
            area = detect_area_for_pharmacy(text) or _get_state(sid).slots.get("area")
            if not area:
                _get_state(sid).slots["area"] = None
                ask = ui.get("ask_pharmacy_area", "Για ποια περιοχή να ψάξω εφημερεύον φαρμακείο; π.χ. Πάτρα, Ρίο, Βραχναίικα, Μεσσάτιδα/Οβρυά, Παραλία Πατρών. 😊")
                return {"reply": enrich_reply(ask, intent=intent)}

            try:
                client = PharmacyClient()
                data = client.get_on_duty(area=area, method="get")
                items = data if isinstance(data, list) else data.get("pharmacies", [])
                if not items:
                    none_msg = ui.get("pharmacy_none_for_area", "❌ Δεν βρέθηκαν εφημερεύοντα για {area}. Θες να δοκιμάσουμε άλλη περιοχή;").format(area=area)
                    return {"reply": enrich_reply(none_msg, intent=intent)}
                _get_state(sid).slots["area"] = area
                _dec_budget(sid)
                reply = f"**Περιοχή: {area}**\n{_format_pharmacies(items)}"
                return {"reply": enrich_reply(reply, intent=intent)}
            except Exception:
                logger.exception("PharmacyClient call failed")
                generic = ui.get("generic_error", "❌ Κάτι πήγε στραβά με την αναζήτηση. Θες να δοκιμάσουμε άλλη περιοχή;")
                return {"reply": enrich_reply(generic, intent=intent)}

        # --- HOSPITAL ---
        if intent == INTENT_HOSPITAL:
            which_day = "σήμερα"
            if "αυρ" in t_norm or "αύρ" in t_norm or "tomorrow" in t_norm:
                which_day = "αύριο"
            _get_state(sid).slots["which_day"] = which_day

            try:
                run_context = {
                    "user_id": body.user_id,
                    "context_text": body.context or "",
                    "history": body.history or [],
                    "system_prompt": SYSTEM_PROMPT,
                    "desired_tool": "hospital_duty",
                }
                result = await Runner.run(chat_agent, input=f"νοσοκομεία {which_day}", context=run_context)
                _dec_budget(sid)
                return {"reply": enrich_reply(result.final_output or "❌ Δεν μπόρεσα να φέρω την εφημερία.", intent=intent)}
            except Exception:
                logger.exception("Hospital intent failed")
                return {"reply": enrich_reply("❌ Δεν κατάφερα να φέρω εφημερεύοντα νοσοκομεία.", intent=intent)}

        # --- TRIP COST ---
        if intent == INTENT_TRIP:
            try:
                # HARD_TRIP_ROUTING: κάλεσε το εργαλείο απευθείας,
                # για να πάρεις είτε Timologio είτε το δικό σου fallback.
                from tools import trip_quote_nlp as _trip_quote_nlp
                reply_raw = _trip_quote_nlp(message=text)
            except Exception:
                logger.exception("trip_quote_nlp local call failed; falling back to agent")
                run_context = {
                    "user_id": body.user_id,
                    "context_text": body.context or "",
                    "history": body.history or [],
                    "system_prompt": SYSTEM_PROMPT,
                    "desired_tool": "trip_quote_nlp",
                }
                result = await Runner.run(chat_agent, input=text, context=run_context)
                reply_raw = result.final_output or "❌ Κάτι πήγε στραβά, να το ξαναπροσπαθήσω;"

            reply, map_url = strip_map_link(reply_raw)
            _dec_budget(sid)
            reply = enrich_reply(reply, intent=intent)
            resp = {"reply": reply}
            if map_url:
                resp["map_url"] = map_url
            return resp



        # --- SERVICES / TOURS
        if intent == INTENT_SERVICES:
            _dec_budget(sid)
            return {"reply": enrich_reply(_format_services_reply(text))}

        # --- INFO / LLM Πάτρας (περιλαμβάνει εκδρομές/ServicesAndToursIntent)
        if intent == INTENT_INFO:
            run_context = {
                "user_id": body.user_id,
                "context_text": body.context or "",
                "history": body.history or [],
                "system_prompt": SYSTEM_PROMPT,
                "desired_tool": "patras_info",
            }
            result = await Runner.run(chat_agent, input=text, context=run_context)
            _dec_budget(sid)
            return {"reply": enrich_reply(result.final_output or "Δεν βρήκα κάτι σχετικό, θες να το ψάξω αλλιώς?")}

        # 3) Γενικό fallback flow
        desired_tool = None
        if predicted_intent in INTENT_TOOL_MAP and score >= 0.70:
            desired_tool = INTENT_TOOL_MAP[predicted_intent]
        elif is_contact_intent(text):
            desired_tool = "taxi_contact"
        elif is_trip_quote(text):
            desired_tool = "trip_quote_nlp"

        if desired_tool == "taxi_contact":
            return {"reply": enrich_reply(_contact_reply(), intent="ContactInfoIntent")}

        # >>>>>> ΝΕΟ: αν είναι trip_quote_nlp, κάλεσέ το απευθείας πρώτα
        if desired_tool == "trip_quote_nlp":
            try:
                from tools import trip_quote_nlp as _trip_quote_nlp
                reply_raw = _trip_quote_nlp(message=text)
                reply, map_url = strip_map_link(reply_raw)
                reply = enrich_reply(reply, intent=INTENT_TRIP)
                resp = {"reply": reply}
                if map_url:
                    resp["map_url"] = map_url
                return resp
            except Exception:
                logger.exception("fallback hard-call trip_quote_nlp failed; using agent")

        # Αλλιώς συνέχισε με τον agent όπως πριν
        run_context = {
            "user_id": body.user_id,
            "context_text": body.context or "",
            "history": body.history or [],
            "system_prompt": SYSTEM_PROMPT,
            "predicted_intent": predicted_intent,
            "desired_tool": desired_tool,
        }
        result = await Runner.run(chat_agent, input=text, context=run_context)
        reply_raw = result.final_output or ""
        reply, map_url = strip_map_link(reply_raw)
        reply = enrich_reply(reply)
        resp = {"reply": reply}
        if map_url:
            resp["map_url"] = map_url
        return resp


    except Exception:
        logger.exception("Agent execution failed")
        origin = request.headers.get("origin", "")
        return JSONResponse(status_code=500, content={"error": "Agent execution error"}, headers=_cors_headers(origin))

# ──────────────────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok"}
