# router_and_booking.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import json
import random
import string
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

# ──────────────────────────────────────────────────────────────────────────────
# Infoxoros integrations (κοστολόγηση, προσφορά, booking link)
try:
    from integrations.infoxoros_api import cost_calculator, get_offer, build_booking_link  # type: ignore
except Exception:
    cost_calculator = None
    get_offer = None
    build_booking_link = None

def _safe_cost_calculator(*args, **kwargs):
    if callable(cost_calculator):
        try:
            return cost_calculator(*args, **kwargs)
        except Exception:
            return None
    return None

def _safe_get_offer(*args, **kwargs):
    if callable(get_offer):
        try:
            return get_offer(*args, **kwargs)
        except Exception:
            return None
    return None

def _safe_booking_link(*args, **kwargs):
    if callable(build_booking_link):
        try:
            return build_booking_link(*args, **kwargs)
        except Exception:
            return None
    return None

# Προαιρετικός geocoder (OSM) από το project — αν λείπει, κάνουμε graceful fallback.
try:
    from tools_geocode import geocode_osm
except Exception:
    geocode_osm = None

# Project tools
from tools import ask_llm, trip_quote_nlp
try:
    # Aggregator ειδοποίησης (Slack/Telegram/Email). Αν δεν υπάρχει, κάν’ το noop.
    from tools import notify_booking  # type: ignore
except Exception:
    try:
        from tools import notify_booking_slack as notify_booking  # type: ignore
    except Exception:
        def notify_booking(_: dict) -> bool:  # type: ignore
            return False

# ──────────────────────────────────────────────────────────────────────────────
# 1) LLM Router
# ──────────────────────────────────────────────────────────────────────────────

ROUTER_SYSTEM = (
    "Είσαι conversation router. Διάβασε το ιστορικό και το νέο μήνυμα. "
    "Βγάλε την πρόθεση και ΟΛΑ τα χρήσιμα slots. ΕΠΙΣΤΡΕΦΕΙΣ ΑΠΟΚΛΕΙΣΤΙΚΑ έγκυρο JSON.\n"
    "intents: [\"TripCost\",\"BaggageCost\",\"ContactInfo\",\"Pharmacy\",\"Hospital\",\"Booking\",\"Smalltalk\",\"Clarify\"]\n"
    "action: [\"answer\",\"ask_missing\",\"call_tool\",\"augment_context\"]\n"
    "slots: {origin,destination,luggage_count,luggage_heavy,area,date_hint,pickup_date,pickup_time,name,phone,pax,notes}\n"
)

SCHEMA_HINT = (
    "Μορφή JSON:\n"
    "{\n"
    "  \"intent\":\"TripCost|BaggageCost|ContactInfo|Pharmacy|Hospital|Booking|Smalltalk|Clarify\",\n"
    "  \"confidence\":0.0,\n"
    "  \"action\":\"answer|ask_missing|call_tool|augment_context\",\n"
    "  \"slots\":{\n"
    "    \"origin\":null,\"destination\":null,\n"
    "    \"luggage_count\":null,\"luggage_heavy\":null,\n"
    "    \"area\":null,\"date_hint\":null,\"pickup_date\":null,\"pickup_time\":null,\n"
    "    \"name\":null,\"phone\":null,\"pax\":null,\"notes\":null\n"
    "  },\n"
    "  \"reason\":\"σύντομη αιτιολόγηση\"\n"
    "}"
)

FOLLOWUP_RE = re.compile(
    r"(αποσκευ|βαλίτσ|βαλιτσ|\bκαι\s+|\bεπίσης\b|^\s*\+?\d+\s*$)|^(ναι|οκ|ok|μάλιστα|σωστά|yes|y)\s*$",
    re.IGNORECASE
)

BOOKING_TRIGGERS = [
    r"\b(κράτηση|κλείσε|κλείσιμο|book|booking|παραλαβή|ραντεβού)\b",
    r"(θέλω|κανόνισε|κλείνω)\s+(ταξί|διαδρομή)",
]

# Σκληρά triggers για ερώτηση κόστους — αν τα δούμε, παγώνουμε όποιο booking flow.
TRIPCOST_TRIGGERS = [
    r"\b(πόσο|κοστίζει|τιμή|κόστος|πόσα)\b.*\b(από|απ'|μέχρι|προς)\b",
    r"\bπόσο\s+πάει\b",
    r"\bδιαδρομ",
]


def _json_coerce(raw: str) -> Dict[str, Any]:
    """Αν το LLM δώσει κείμενο γύρω από το JSON, κόψε το καθαρό αντικείμενο."""
    try:
        return json.loads(raw)
    except Exception:
        s = raw.find('{'); e = raw.rfind('}')
        if s != -1 and e != -1 and e > s:
            return json.loads(raw[s:e+1])
        return {"intent": "Clarify", "confidence": 0.0, "action": "ask_missing", "slots": {}, "reason": "parse_error"}


def llm_route(context_text: str, user_msg: str) -> Dict[str, Any]:
    prompt = (
        f"{SCHEMA_HINT}\n\n"
        f"ΙΣΤΟΡΙΚΟ:\n{context_text}\n\n"
        f"ΜΗΝΥΜΑ ΧΡΗΣΤΗ:\n{user_msg}\n\n"
        "Κάνε robust extraction. Αν λείπουν πράγματα για Trip/Booking, βάλε action=\"ask_missing\".\n"
        "Αν το μήνυμα μοιάζει με επιβεβαίωση (ναι/οκ), προσπάθησε να καταλάβεις σε ποια τελευταία προσφορά αναφέρεται."
    )
    try:
        raw = ask_llm(prompt, system=ROUTER_SYSTEM, temperature=0)
    except Exception:
        return {"intent": "Clarify", "confidence": 0.0, "action": "ask_missing", "slots": {}, "reason": "ask_llm_error"}
    return _json_coerce(raw)


# ──────────────────────────────────────────────────────────────────────────────
# 2) Session helpers
# ──────────────────────────────────────────────────────────────────────────────

def init_session_state(st: Any) -> None:
    if not hasattr(st, "last_offered"): setattr(st, "last_offered", None)
    if not hasattr(st, "pending_trip"): setattr(st, "pending_trip", {})
    if not hasattr(st, "context_turns"): setattr(st, "context_turns", [])
    if not hasattr(st, "booking_slots"): setattr(st, "booking_slots", {})
    if not hasattr(st, "slots"): setattr(st, "slots", {})
    if not hasattr(st, "timestamps"): setattr(st, "timestamps", {})


def _reset_booking(st: Any) -> None:
    st.intent = None
    st.booking_slots = {}
    st.last_offered = None


def looks_like_followup(text: str) -> bool:
    return bool(FOLLOWUP_RE.search(text.strip()))


# ──────────────────────────────────────────────────────────────────────────────
# 3) Baggage policy / Trip merge
# ──────────────────────────────────────────────────────────────────────────────

BAGGAGE_NOTE = "Αποσκευές έως 10kg: χωρίς επιβάρυνση. >10kg: +0,39€/τεμάχιο."

def _yesish(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in {"true", "yes", "1", "ναι", "βαριές", "βαριες", "heavy"}


def baggage_policy_reply(st: Any) -> Dict[str, Any]:
    count = st.pending_trip.get("luggage_count")
    heavy = st.pending_trip.get("luggage_heavy")

    extra = 0.0
    if isinstance(count, int) and count > 0 and _yesish(heavy):
        extra = round(count * 0.39, 2)

    lines = [BAGGAGE_NOTE]
    if extra > 0:
        lines.append(f"Για {count} βαριές αποσκευές: ~{extra:.2f}€ συνολικά.")
    if st.pending_trip.get("origin") and st.pending_trip.get("destination"):
        lines.append("Θες να το προσθέσω στην εκτίμηση διαδρομής;")

    st.last_offered = "baggage_cost_info"
    return {"reply": "\n".join(lines)}


# ──────────────────────────────────────────────────────────────────────────────
# 3a) Address precision helpers
# ──────────────────────────────────────────────────────────────────────────────

POI_OK_PAT = re.compile(
    r"(νοσοκομ|αεροδρομ|κτελ|ktel|σταθμ|λιμάνι|port|πανεπιστ|university|campus)",
    re.IGNORECASE
)

def is_precise_address(s: str) -> bool:
    """Θεωρούμε ακριβές: έχει αριθμό (οδός & αριθμός) ή γνωστό POI (νοσοκομείο κ.λπ.)."""
    if not s:
        return False
    s = s.strip()
    return bool(re.search(r"\d", s)) or bool(POI_OK_PAT.search(s))


def refine_prompt(kind: str) -> str:
    # kind: "origin" | "destination"
    return "Δώσε **ακριβή διεύθυνση** {} (οδός & αριθμός ή γνωστό σημείο π.χ. Νοσοκομείο Ρίο).".format(
        "παραλαβής" if kind == "origin" else "προορισμού"
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3b) Geocoding (OSM) — με fallback HTTP αν δεν υπάρχει project geocoder
# ──────────────────────────────────────────────────────────────────────────────

try:
    import requests as _rq  # type: ignore
except Exception:
    _rq = None

def _geocode_fallback(q: str) -> Optional[tuple[float, float]]:
    if not _rq:
        return None
    try:
        r = _rq.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": "MrBooky/1.0 (+taxi)"},
            timeout=8
        )
        r.raise_for_status()
        j = r.json()
        if not j:
            return None
        return float(j[0]["lat"]), float(j[0]["lon"])
    except Exception:
        return None


def _resolve_coords(origin: str, destination: str) -> Optional[tuple[tuple[float, float], tuple[float, float]]]:
    # Προτίμησε project geocoder, αλλιώς fallback σε Nominatim
    if geocode_osm:
        try:
            o_lat, o_lon = geocode_osm(origin)
            d_lat, d_lon = geocode_osm(destination)
            return (o_lat, o_lon), (d_lat, d_lon)
        except Exception:
            pass
    o = _geocode_fallback(origin)
    d = _geocode_fallback(destination)
    if o and d:
        return (o[0], o[1]), (d[0], d[1])
    return None


def _append_infoxoros_estimate_if_possible(st, reply_text: str) -> str:
    """Εμπλούτισε την απάντηση με estimate από το Infoxoros cost_calculator, αν έχουμε συντεταγμένες."""
    try:
        origin = st.pending_trip.get("origin") or st.slots.get("last_origin")
        dest = st.pending_trip.get("destination") or st.slots.get("last_dest")
        if not origin or not dest:
            return reply_text

        # Ώρα παραλαβής (HH:MM) αν υπάρχει
        hhmm = None
        pt = st.pending_trip.get("pickup_time") or st.pending_trip.get("time")
        if pt and isinstance(pt, str):
            m = re.search(r"(\d{1,2}:\d{2})", pt)
            if m:
                hhmm = m.group(1)

        coords = _resolve_coords(origin, dest)
        if not coords:
            return reply_text  # χωρίς geocoder, προχώρησε χωρίς εμπλουτισμό

        (o_lat, o_lon), (d_lat, d_lon) = coords
        est = _safe_cost_calculator(lat_start=o_lat, lon_start=o_lon, lat_end=d_lat, lon_end=d_lon, time_hhmm=hhmm)
        cost = est.get("cost_float"); dkm = est.get("distance_km"); dmin = est.get("duration_min")
        if cost:
            line = f"\n\n🧷 *Infoxoros estimate*: ~{cost:.2f}€"
            if dkm is not None:
                line += f" • {dkm:.1f}km"
            if dmin is not None:
                line += f" • ~{dmin}′"
            if hhmm:
                line += f" • {hhmm}"
            return reply_text + line
        return reply_text
    except Exception:
        return reply_text


def run_trip_quote_with_luggage(st: Any) -> Dict[str, Any]:
    origin = st.pending_trip.get("origin")
    destination = st.pending_trip.get("destination")
    if not origin or not destination:
        return {"reply": "Πες μου αφετηρία και προορισμό για να δώσω εκτίμηση."}

    q = trip_quote_nlp(f"από {origin} μέχρι {destination}")

    extra = 0.0
    cnt = st.pending_trip.get("luggage_count")
    heavy = st.pending_trip.get("luggage_heavy")
    if isinstance(cnt, int) and cnt > 0 and _yesish(heavy):
        extra = round(cnt * 0.39, 2)

    reply = q.get("reply") if isinstance(q, dict) else str(q)
    if extra > 0:
        reply += f"\n\n🧳 Εκτίμηση επιπλέον για αποσκευές: ~{extra:.2f}€\n({BAGGAGE_NOTE})"

    reply = _append_infoxoros_estimate_if_possible(st, reply)

    st.last_offered = "trip_quote"
    st.last_tool = "trip_quote_nlp"
    st.last_trip_query = {
        "origin": origin,
        "destination": destination,
        "luggage_count": cnt,
        "luggage_heavy": heavy,
    }
    return {"reply": reply}


def ask_for_missing_slots(pending: Dict[str, Any]) -> Dict[str, Any]:
    if not pending.get("origin"):
        return {"reply": "Ποια είναι η αφετηρία;"}
    if not pending.get("destination"):
        return {"reply": "Ποιος είναι ο προορισμός;"}
    return {"reply": "Πες μου αφετηρία και προορισμό."}


# ──────────────────────────────────────────────────────────────────────────────
# 4) Booking slot-filling (με “ακριβή διεύθυνση”)
# ──────────────────────────────────────────────────────────────────────────────

RE_PHONE = re.compile(r"\+?\d{10,15}")
BOOKING_REQUIRED = ["origin", "destination", "pickup_time", "name", "phone"]
BOOKING_OPTIONAL = ["pax", "luggage_count", "luggage_heavy", "notes", "pickup_date"]


def normalize_phone(s: str) -> Optional[str]:
    if not s:
        return None
    s2 = s.replace(" ", "")
    m = RE_PHONE.search(s2)
    return m.group(0) if m else None


def parse_pickup_time(text: str) -> str:
    t = (text or "").strip().lower()
    if any(x in t for x in ["άμεσα", "αμεσα", "τωρα", "τώρα", "now", "asap"]):
        return "ASAP"
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", t)
    if m:
        hh, mm = m.group(1), m.group(2)
        return f"{hh}:{mm}"
    return ""


_DAYS = {
    "δευ": 0, "δευτέρα": 0, "δευτερα": 0,
    "τρι": 1, "τρίτη": 1, "τριτη": 1,
    "τετ": 2, "τετάρτη": 2, "τεταρτη": 2,
    "πεμ": 3, "πέμπτη": 3, "πεμπτη": 3,
    "παρ": 4, "παρασκευή": 4, "παρασκευη": 4,
    "σαβ": 5, "σάββατο": 5, "σαββατο": 5,
    "κυρ": 6, "κυριακή": 6, "κυριακη": 6
}

def parse_date_hint(text: str) -> Optional[str]:
    t = (text or "").strip().lower()
    now = datetime.now()
    if any(w in t for w in ["σήμερα", "σημερα", "today"]):
        return now.strftime("%Y-%m-%d")
    if any(w in t for w in ["αύριο", "αυριο", "tomorrow"]):
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    if any(w in t for w in ["μεθαύριο", "μεθαυριο"]):
        return (now + timedelta(days=2)).strftime("%Y-%m-%d")
    for key, dow in _DAYS.items():
        if key in t:
            delta = (dow - now.weekday()) % 7
            delta = delta or 7
            return (now + timedelta(days=delta)).strftime("%Y-%m-%d")
    m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", t)
    if m:
        return m.group(1)
    return None


def _extract_from_to(text: str) -> tuple[Optional[str], Optional[str]]:
    s = (text or "").strip()
    s = re.sub(r"[;;?!…]+$", "", s)
    m = re.search(r"απ[όο]\s+(.+?)\s+(?:μέχρι|προς|στον|στο|στην|στη)\s+(.+)$", s, flags=re.IGNORECASE)
    if m:
        o = m.group(1).strip()
        d = m.group(2).strip()
        return (o, d)
    return (None, None)


def next_missing_booking_slot(slots: Dict[str, Any]) -> Optional[str]:
    for k in BOOKING_REQUIRED:
        if not slots.get(k):
            return k
    return None


PROMPTS = {
    "origin": "Από πού σε παραλαμβάνουμε; (οδός & αριθμός ή γνωστό σημείο)",
    "destination": "Πού πας; (οδός & αριθμός ή γνωστό σημείο)",
    "pickup_time": "Πότε θες παραλαβή; (γράψε ‘άμεσα’ ή ώρα π.χ. 18:30)",
    "name": "Πώς σε λένε;",
    "phone": "Ποιο είναι το κινητό σου; (για επιβεβαίωση οδηγού)",
}


def booking_prompt_next(st: Any) -> Dict[str, Any]:
    ask = next_missing_booking_slot(st.booking_slots) or "origin"
    # Αν ζητάμε origin/destination, υπενθύμισε “οδός & αριθμός”
    if ask in ("origin", "destination"):
        return {"reply": refine_prompt(ask)}
    return {"reply": PROMPTS[ask]}


def booking_start(st: Any, *, reset: bool = True, source_text: Optional[str] = None) -> Dict[str, Any]:
    st.intent = "BookingIntent"
    if reset:
        st.booking_slots = {}
    # Prefill από αρχικό μήνυμα ΜΟΝΟ αν είναι ακριβές
    if source_text:
        o, d = _extract_from_to(source_text)
        if o and is_precise_address(o):
            st.booking_slots["origin"] = o
        if d and is_precise_address(d):
            st.booking_slots["destination"] = d
        dh = parse_date_hint(source_text)
        if dh:
            st.booking_slots["pickup_date"] = dh
        pt = parse_pickup_time(source_text)
        if pt:
            st.booking_slots["pickup_time"] = pt
    return booking_prompt_next(st)


def booking_collect(st: Any, user_text: str) -> Dict[str, Any]:
    slots = st.booking_slots
    missing = next_missing_booking_slot(slots)
    val = (user_text or "").strip()

    # Πιάσε πιθανή ημερομηνία σε κάθε βήμα
    dh = parse_date_hint(val)
    if dh:
        slots["pickup_date"] = dh

    if missing == "phone":
        phone = normalize_phone(val)
        if not phone:
            return {"reply": "Δώσε μου ένα κινητό (π.χ. +3069…)"}
        slots["phone"] = phone

    elif missing == "pickup_time":
        parsed = parse_pickup_time(val)
        if not parsed:
            return {"reply": "Γράψε ‘άμεσα’ ή μια ώρα π.χ. 18:30"}
        slots["pickup_time"] = parsed

    elif missing in {"origin", "destination"}:
        if not val:
            return booking_prompt_next(st)
        if not is_precise_address(val):
            return {"reply": refine_prompt(missing)}
        slots[missing] = val

    elif missing == "name":
        if not val:
            return booking_prompt_next(st)
        slots["name"] = val

    # Δεν κάνουμε reset! Προχωράμε στο επόμενο πεδίο ή σύνοψη.
    nxt = next_missing_booking_slot(slots)
    if nxt:
        return booking_prompt_next(st)
    return booking_confirm(st)


def booking_confirm(st: Any) -> Dict[str, Any]:
    s = st.booking_slots
    quote_reply = ""
    try:
        q = trip_quote_nlp(f"από {s['origin']} μέχρι {s['destination']}")
        if isinstance(q, dict) and "reply" in q:
            quote_reply = f"\n\n{q['reply']}"
        elif isinstance(q, str):
            quote_reply = f"\n\n{q}"
    except Exception:
        pass

    summary = (
        "📋 **Σύνοψη κράτησης**\n"
        f"- Από: {s['origin']}\n- Προς: {s['destination']}\n"
        f"- Ημερομηνία: {s.get('pickup_date','(σήμερα)')}\n"
        f"- Ώρα: {s['pickup_time']}\n"
        f"- Όνομα: {s['name']}\n- Κινητό: {s['phone']}\n"
        f"- Άτομα: {s.get('pax','1')}\n"
        f"- Αποσκευές: {s.get('luggage_count','0')} (βαριές: {s.get('luggage_heavy','όχι')})\n"
        f"{quote_reply}\n\nΝα προχωρήσω την κράτηση; (ναι/όχι)"
    )
    st.last_offered = "booking_confirm"
    return {"reply": summary}


def _booking_code() -> str:
    rnd = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"BK-{datetime.now().strftime('%Y%m%d')}-{rnd}"


def _compose_when(s: Dict[str, Any]) -> str:
    """Παράγει 'YYYY-MM-DD HH:MM:SS' από pickup_date/pickup_time/ASAP."""
    dt_full = s.get("pickup_datetime")
    if isinstance(dt_full, str) and re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}", dt_full):
        return dt_full[:19]

    date = s.get("pickup_date")
    time_s = s.get("pickup_time")

    if isinstance(date, str) and isinstance(time_s, str) and re.match(r"^\d{1,2}:\d{2}$", time_s):
        hh, mm = time_s.split(":")
        return f"{date} {hh.zfill(2)}:{mm}:00"

    if isinstance(time_s, str):
        if time_s.upper() == "ASAP":
            return (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        m = re.match(r"^(\d{1,2}):(\d{2})$", time_s)
        if m:
            today = datetime.now().strftime("%Y-%m-%d")
            return f"{today} {m.group(1).zfill(2)}:{m.group(2)}:00"

    # fallback: τώρα+20'
    return (datetime.now() + timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")


def booking_finalize(st: Any) -> Dict[str, Any]:
    from integrations.infoxoros_api import create_booking, build_booking_link
    s = st.booking_slots
    code = _booking_code()
    origin = s.get("origin")
    dest = s.get("destination")
    when = _compose_when(s)

    # γεωκωδικοποίηση για point1/point2
    coords = _resolve_coords(origin, dest)  # μπορεί να είναι None
    point1 = point2 = None
    if coords:
        (o_lat, o_lon), (d_lat, d_lon) = coords
        point1 = f"{o_lat},{o_lon}"
        point2 = f"{d_lat},{d_lon}"

    created_remote = False
    info_line = ""
    # Προσπάθησε ΠΛΗΡΗ υποβολή (action=create) μόνο αν έχουμε coords
    if point1 and point2:
        try:
            res = create_booking(
                origin_address=origin,
                destination_address=dest,
                point1=point1, point2=point2,
                when_iso=when,
                name=s.get("name",""),
                phone=s.get("phone",""),
                email=s.get("email",""),
                pax=int(s.get("pax", 1) or 1),
                luggage_count=int(s.get("luggage_count", 0) or 0),
                remarks=s.get("notes",""),
                # captcha=None  # αν χρειαστεί και μπορεί να παραχθεί client-side, πρόσθεσέ το εδώ
            )
            # Το backend συνήθως επιστρέφει {status: 1, ...} στο success
            created_remote = str(res.get("status", "0")) == "1"
            info_line = "✅ Δημιουργήθηκε στο σύστημα." if created_remote else "ℹ️ Το σύστημα δεν επιβεβαίωσε τη δημιουργία."
        except Exception as e:
            info_line = "ℹ️ Σφάλμα υποβολής create — συνεχίζουμε με προ-κράτηση."
            created_remote = False
    else:
        info_line = "ℹ️ Δεν μπόρεσα να κάνω geocoding — συνεχίζουμε με προ-κράτηση."

    link = _safe_booking_link(lang="el")

    pax = s.get("pax", 1)
    lug = s.get("luggage_count", 0)
    heavy = s.get("luggage_heavy", "όχι")
    notes = s.get("notes", "")
    pickup_date = s.get("pickup_date", datetime.now().strftime("%Y-%m-%d"))
    copy_block = (
        f"ΚΩΔΙΚΟΣ: {code}\n"
        f"Παραλαβή: {origin}\n"
        f"Προορισμός: {dest}\n"
        f"Ημερομηνία: {pickup_date}\n"
        f"Ώρα: {s.get('pickup_time')}\n"
        f"Όνομα: {s.get('name')}\n"
        f"Κινητό: {s.get('phone')}\n"
        f"Email: {s.get('email','')}\n"
        f"Άτομα: {pax}\n"
        f"Αποσκευές: {lug} (βαριές: {heavy})\n"
        f"Σημειώσεις: {notes}"
    ).strip()

    if created_remote:
        status_line = f"✅ Η κράτηση δημιουργήθηκε στο σύστημα. Κωδικός: {code}"
    else:
        status_line = (
            f"📝 Προ-κράτηση καταγράφηκε (εσωτερικά). Κωδικός: {code}\n"
            f"➡️ Για ολοκλήρωση στο σύστημα, άνοιξε τον σύνδεσμο και υπέβαλε τη φόρμα (captcha)."
        )

    reply = (
        f"{status_line}\n"
        f"{info_line}\n"
        f"🔗 Ολοκλήρωση: {link}\n\n"
        f"📋 **Copy-paste στη φόρμα**:\n{copy_block}"
    )

    # Ειδοποίηση back-office να μη χαθεί το lead
    try:
        notify_booking({
            "code": code,
            "origin": origin, "destination": dest,
            "pickup_time": when,
            "pax": pax,
            "luggage_count": lug,
            "luggage_heavy": _yesish(heavy),
            "name": s.get("name",""),
            "phone": s.get("phone",""),
            "email": s.get("email",""),
            "notes": notes,
            "created_remote": created_remote,
        })
    except Exception:
        pass

    st.last_offered = None
    return {"reply": reply}


# ──────────────────────────────────────────────────────────────────────────────
# 5) Router-based follow-up entry point
# ──────────────────────────────────────────────────────────────────────────────

def maybe_handle_followup_or_booking(st: Any, user_text: str) -> Optional[Dict[str, Any]]:
    """
    Entry point που μπορεί να καλεστεί από το main πριν/μετά το intent routing.
    Επιστρέφει dict(reply=...) αν χειρίζεται το μήνυμα εδώ, αλλιώς None για να συνεχίσει ο main.
    """
    init_session_state(st)
    txt = (user_text or "").strip()

    # A) Αν ο χρήστης ρωτά για ΚΟΣΤΟΣ → κόψε τυχόν stale booking & άφησε τον main να απαντήσει με TripCost
    if any(re.search(p, txt, re.IGNORECASE) for p in TRIPCOST_TRIGGERS):
        _reset_booking(st)
        return None

    is_short = txt.lower() in {"ναι", "οκ", "ok", "μάλιστα", "σωστά", "yes", "y"}
    booking_intent = any(re.search(p, txt, re.IGNORECASE) for p in BOOKING_TRIGGERS)

    # B) Direct confirms (χωρίς LLM)
    if is_short:
        if st.last_offered == "trip_quote":
            return run_trip_quote_with_luggage(st)
        if st.last_offered == "booking_confirm":
            return booking_finalize(st)
        if st.last_offered == "baggage_cost_info" and st.pending_trip.get("origin") and st.pending_trip.get("destination"):
            return run_trip_quote_with_luggage(st)

    # C) Νέο booking → καθαρό ξεκίνημα + prefill από το ίδιο μήνυμα (μόνο ακριβή πεδία)
    if booking_intent and st.intent != "BookingIntent":
        return booking_start(st, reset=True, source_text=txt)

    # D) Συνεχόμενο booking
    if st.intent == "BookingIntent":
        return booking_collect(st, txt)

    # E) Γρήγορο baggage χωρίς LLM
    if re.search(r"αποσκευ|βαλίτσ|βαλιτσ", txt, re.IGNORECASE):
        m = re.search(r"(\d+)", txt)
        if m:
            st.pending_trip["luggage_count"] = int(m.group(1))
        if re.search(r"βαρι(ές|α)|heavy", txt, re.IGNORECASE):
            st.pending_trip["luggage_heavy"] = True
        return baggage_policy_reply(st)

    # F) LLM routing για λοιπά follow-ups (π.χ. ο χρήστης δίνει νέα στοιχεία σε ελεύθερο κείμενο)
    context = "\n".join((st.context_turns or [])[-8:])
    route = llm_route(context, txt)
    intent = (route.get("intent") or "").strip()
    slots = route.get("slots") or {}

    # Συγχώνευση slots στο pending_trip
    for k, v in slots.items():
        if v not in (None, ""):
            st.pending_trip[k] = v
            if k == "luggage_heavy" and isinstance(v, str):
                st.pending_trip[k] = v.lower() in {"true", "yes", "1", "ναι"}
            if k == "luggage_count" and isinstance(v, str) and v.isdigit():
                st.pending_trip[k] = int(v)

    # Intent-based
    if intent == "BaggageCost" or ("luggage_count" in st.pending_trip or "luggage_heavy" in st.pending_trip):
        return baggage_policy_reply(st)

    if intent == "TripCost":
        if not st.pending_trip.get("origin") or not st.pending_trip.get("destination"):
            return ask_for_missing_slots(st.pending_trip)
        return run_trip_quote_with_luggage(st)

    if intent == "Booking":
        # Μην σβήνεις τυχόν προ-συμπλήρωση
        return booking_start(st, reset=False)

    return None
