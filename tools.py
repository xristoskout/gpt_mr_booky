from __future__ import annotations

"""
This module defines a collection of helper functions and tool wrappers used by the
Mr Booky assistant. It handles text normalization, tariff calculations, route
parsing, trip estimation, taxi contact information, pharmacy and hospital
lookups, and interactions with the LLM. The file is designed to be imported
by the main application and can fall back gracefully when optional
dependencies are missing. The functions decorated with ``@function_tool`` are
intended to be exposed to the agent runtime as callable tools.

Enhancements in this version:

* Added strict JSON Schema-based tools: ``resolve_place`` and ``estimate_fare``.
  These enforce structured arguments and predictable outputs.
* Fallback-safe decorator now preserves ``strict`` and ``parameters`` metadata
  even when the Agents SDK is not installed.
* ``trip_quote_nlp`` first uses the strict tools pipeline (resolve → estimate)
  and gracefully falls back to Timologio API or rough estimates.
* Optional runtime validation via ``jsonschema`` with a light fallback checker.
* Small utilities (Haversine, normalization) and a tiny local gazetteer for Patras.

The rest of the module closely follows the original code structure, with
guarded imports and fallbacks to ensure the assistant can operate even when
third-party clients or LLM backends are unavailable.
"""

import os
import requests
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import unicodedata
from urllib.parse import quote_plus

# ──────────────────────────────────────────────────────────────────────────────
# Agents SDK (with compatibility shim)
try:
    # Import agent helper types from the Agents SDK if available.
    from agents import function_tool as _sdk_function_tool, RunContextWrapper  # type: ignore

    def function_tool(fn=None, **kwargs):
        """Compatibility decorator that survives older/newer SDKs.
        - Filters unknown kwargs for the SDK call.
        - Tries `strict=False` first to avoid SDK-level strict schema crashes.
        - If SDK decoration fails, falls back to returning the original function.
        - Always attaches metadata attributes: name/description/parameters/strict.
        """
        allowed = {"name_override", "description_override"}
        safe = {k: v for k, v in kwargs.items() if k in allowed}

        def _decorate_with_sdk(f):
            # Attempt with strict=False (newer SDKs may accept), then without.
            try:
                return _sdk_function_tool(**safe, strict=False)(f)  # type: ignore[call-arg]
            except TypeError:
                # Older SDKs: no `strict` kw
                return _sdk_function_tool(**safe)(f)

        def _attach_meta(obj, f):
            # Ensure friendly name/description even if SDK wraps the function
            if "name_override" in kwargs:
                try:
                    setattr(obj, "name", kwargs["name_override"])  # for discovery UIs
                except Exception:
                    pass
            if "description_override" in kwargs:
                try:
                    setattr(obj, "description", kwargs["description_override"])  # for discovery UIs
                except Exception:
                    pass
            # Preserve strict + parameters metadata for our runtime
            if "strict" in kwargs:
                try:
                    setattr(obj, "__strict__", bool(kwargs["strict"]))
                except Exception:
                    pass
            if "parameters" in kwargs and kwargs["parameters"]:
                try:
                    setattr(obj, "__parameters__", kwargs["parameters"])  # JSON Schema dict
                except Exception:
                    pass
            return obj

        if fn is None:
            def _wrap(f):
                try:
                    obj = _decorate_with_sdk(f)
                except Exception:
                    # Fall back to the raw function if the SDK chokes on schema inference
                    obj = f
                return _attach_meta(obj, f)
            return _wrap
        else:
            try:
                obj = _decorate_with_sdk(fn)
            except Exception:
                obj = fn
            return _attach_meta(obj, fn)
except Exception:  # graceful fallback if Agents SDK missing
    def function_tool(fn=None, **kwargs):
        """A minimal decorator to attach metadata for tools when the
        Agents SDK is not available. Keeps name/description/parameters/strict
        as attributes on the underlying function so runtime can still inspect
        and validate calls.
        """
        def _decorator(f):
            # User-provided name/description fallbacks
            setattr(f, "name", kwargs.get("name_override", getattr(f, "__name__", "tool")))
            setattr(f, "description", kwargs.get("description_override", getattr(f, "__doc__", "")))
            # Preserve strict + parameters metadata
            if "strict" in kwargs:
                setattr(f, "__strict__", bool(kwargs["strict"]))
            if "parameters" in kwargs and kwargs["parameters"]:
                setattr(f, "__parameters__", kwargs["parameters"])  # JSON Schema dict
            return f

        if fn is None:
            return _decorator
        return _decorator(fn)

    class RunContextWrapper:  # minimal placeholder to satisfy type hints
        def __init__(self, context=None, **kwargs):
            self.context = context or {}

# Unicode normalization helpers
from unicodedata import normalize as _u_norm

# Optional OpenAI client (for ask_llm)
try:
    from openai import OpenAI  # type: ignore
except Exception:  # optional dependency
    OpenAI = None  # type: ignore

from phrases import pick_trendy_phrase  # trendy phrase picker, optional
from constants import TAXI_TARIFF  # tariff configuration
import constants

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Brand / Config

BRAND_INFO: Dict[str, Any] = getattr(constants, "BRAND_INFO", {})
DEFAULTS: Dict[str, Any] = getattr(constants, "DEFAULTS", {})
UI_TEXT: Dict[str, str] = getattr(constants, "UI_TEXT", {})
AREA_ALIASES: Dict[str, List[str]] = getattr(constants, "AREA_ALIASES", {})

# Q tails που κολλάνε στο destination (GR & greeklish)
_Q_TAIL_GR = r"(?:\bπόσο(?:\s+κοστίζει)?\b|\bκοστίζει\b|\bκάνει\b|\bτιμή\b|\?)\s*$"
_Q_TAIL_GL = r"(?:\bposo(?:\s+kostizei)?\b|\bkostizei\b|\bkanei\b|\btimi\b|\?)\s*$"

# Stopwords που ΔΕΝ πρέπει να θεωρηθούν origin/destination
_ROUTE_STOPWORDS = {"πόσο", "απο", "μεχρι","εως","εωσ", "κοστίζει", "κάνει", "τιμή", "poso","from", "kostizei", "kanei", "timi"}
# Default origin used when user specifies only a destination.
# Feel free to adjust this string (π.χ. σε "Πάτρα") αν θέλετε άλλη προεπιλεγμένη αφετηρία.
DEFAULT_ORIGIN = "Πάτρα"

# ──────────────────────────────────────────────────────────────────────────────
# Strict JSON Schema support (optional jsonschema)
from typing import TypedDict  # noqa: E402
try:
    import jsonschema  # strict runtime validation
except Exception:  # pragma: no cover
    jsonschema = None  # type: ignore

# Haversine helper για απόσταση km
from math import radians, sin, cos, asin, sqrt  # noqa: E402

def _haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    R = 6371.0088
    dlat = radians(b_lat - a_lat)
    dlng = radians(b_lng - a_lng)
    aa = sin(dlat/2) ** 2 + cos(radians(a_lat)) * cos(radians(b_lat)) * sin(dlng/2) ** 2
    return float(2 * R * asin(sqrt(aa)))

# JSON Schema validators (jsonschema if present; else light checks)

def _validate_with_schema(payload: dict, schema: dict, *, where: str = "") -> None:
    if jsonschema is not None:
        jsonschema.validate(payload, schema)  # raises on error
        return
    # Fallback: required + additionalProperties=False
    req = set(schema.get("required", []))
    if not req.issubset(payload.keys()):
        missing = req - set(payload.keys())
        raise ValueError(f"Missing required {missing} in {where or 'payload'}")
    if schema.get("additionalProperties") is False:
        allowed = set(schema.get("properties", {}).keys())
        extra = set(payload.keys()) - allowed
        if extra:
            raise ValueError(f"Unexpected properties {extra} in {where or 'payload'}")

# ──────────────────────────────────────────────────────────────────────────────
# Helpers: text normalization

def _deaccent(s: str) -> str:
    """Remove diacritics from a string."""
    if not s:
        return ""
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def _norm_txt(s: str) -> str:
    """Normalize, lowercase, and strip diacritics and excess whitespace."""
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
# Tariff helpers (συγχρονισμένα με constants.py)

def _tariff(keys: List[str], default: float) -> float:
    for k in keys:
        try:
            v = TAXI_TARIFF.get(k)
            if v is not None:
                return float(v)
        except Exception:
            pass
    return float(default)


DAY_KM = _tariff(["km_rate_city_or_day", "km_rate_zone1"], 0.90)
NIGHT_KM = _tariff(["km_rate_zone2_or_night"], max(DAY_KM, 1.25))
START_FEE = _tariff(["minimum_fare"], 4.0)

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
        for h in history[-2:]:  # cut down history to reduce cost/PII
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
# JSON Schemas for strict tools

PLACE_OBJECT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "place_id": {"type": "string", "minLength": 1},
        "lat": {"type": "number"},
        "lng": {"type": "number"},
        "name": {"type": "string"},
        "address": {"type": "string"},
    },
    "required": ["place_id", "lat", "lng"],
}

RESOLVE_PLACE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "city": {"type": "string", "default": "Πάτρα"},
    },
    "required": ["query"],
}

ESTIMATE_FARE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "origin": PLACE_OBJECT_SCHEMA,
        "destination": PLACE_OBJECT_SCHEMA,
        "when": {"type": "string", "enum": ["day", "night"], "default": "day"},
        "round_trip": {"type": "boolean", "default": False},
    },
    "required": ["origin", "destination"],
}

# ──────────────────────────────────────────────────────────────────────────────
# Tiny local gazetteer for Patras (extensible)

_GAZETTEER: Dict[str, Dict[str, Any]] = {
    # Adjust coordinates if you have more precise values
    "new_port_patras": {
        "aliases": [
            "νέο λιμάνι", "νεο λιμανι", "new port", "south port", "akti dimaion", "ακτή δυμαίων",
        ],
        "lat": 38.22655,
        "lng": 21.72131,
        "name": "Νέο Λιμάνι Πάτρας",
        "address": "Ακτή Δυμαίων, Πάτρα 263 33",
    },
    "ktel_achaias": {
        "aliases": [
            "κτελ", "κτελ πατρας", "ktel achaias", "patras bus station", "ζαΐμη 2", "zaimi 2",
        ],
        # Provide best-known coords
        "lat": 38.2448,
        "lng": 21.7349,
        "name": "ΚΤΕΛ Αχαΐας",
        "address": "Ζαΐμη 2 & Όθωνος Αμαλίας, Πάτρα 262 22",
    },
}

_alias_to_pid: Dict[str, str] = {}
for _pid, _rec in _GAZETTEER.items():
    for _a in _rec.get("aliases", []) or []:
        _alias_to_pid[_norm_txt(_a)] = _pid


def _lookup_gazetteer(q: str) -> Optional[Dict[str, Any]]:
    key = _norm_txt(q)
    pid = _alias_to_pid.get(key)
    if not pid:
        # try removing Greek articles
        key2 = re.sub(r"^(το|η|ο|τα|οι)\s+", "", key)
        pid = _alias_to_pid.get(key2)
    if pid:
        r = _GAZETTEER[pid]
        return {
            "place_id": pid,
            "lat": r["lat"],
            "lng": r["lng"],
            "name": r.get("name", pid),
            "address": r.get("address", ""),
        }
    return None

# ──────────────────────────────────────────────────────────────────────────────
# Geocoding (OSM/Nominatim)

def geocode_osm(q: str) -> Tuple[float, float]:
    """Geocode an address using OpenStreetMap's Nominatim API."""
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": q, "format": "jsonv2", "limit": 1},
        headers={"User-Agent": "MrBooky/1.0 (+taxi)"},
        timeout=8,
    )
    r.raise_for_status()
    j = r.json()
    if not j:
        raise ValueError(f"Not found: {q}")
    return float(j[0]["lat"]), float(j[0]["lon"])

# ──────────────────────────────────────────────────────────────────────────────
# Strict tools

@function_tool(
    name_override="resolve_place",
    description_override="Resolve a place in/near Patras to a canonical object with place_id/lat/lng.",
    parameters=RESOLVE_PLACE_SCHEMA,
    strict=True,
)
def resolve_place(query: str, city: str = "Πάτρα") -> Dict[str, Any]:
    """
    Επιστρέφει αντικείμενο {place_id, lat, lng, name, address}.
    Πρώτα ψάχνει στο τοπικό gazetteer, μετά δοκιμάζει OSM/Nominatim.
    """
    payload = {"query": query, "city": city}
    _validate_with_schema(payload, RESOLVE_PLACE_SCHEMA, where="resolve_place.args")

    # 1) Gazetteer hit
    hit = _lookup_gazetteer(query)
    if hit:
        return hit

    # 2) Fallback: OSM geocoding with city bias
    q = f"{query}, {city}" if city and _norm_txt(city) not in _norm_txt(query) else query
    lat, lng = geocode_osm(q)  # raises if not found
    return {
        "place_id": f"osm:{_norm_txt(query)[:48]}",
        "lat": lat,
        "lng": lng,
        "name": query.strip(),
        "address": f"{query.strip()}, {city}".strip().strip(","),
    }

# Attach metadata for fallback runtime (in case Agents SDK isn't installed)
try:
    resolve_place.__strict__ = True
    resolve_place.__parameters__ = RESOLVE_PLACE_SCHEMA
except Exception:
    pass


@function_tool(
    name_override="estimate_fare",
    description_override="Estimate taxi fare between two resolved places in/near Patras.",
    parameters=ESTIMATE_FARE_SCHEMA,
    strict=True,
)
def estimate_fare(
    origin: Dict[str, Any],
    destination: Dict[str, Any],
    when: str = "day",
    round_trip: bool = False,
) -> Dict[str, Any]:
    """
    Υπολογίζει κομίστρα με βάση START_FEE/DAY_KM/NIGHT_KM.
    Επιστρέφει {distance_km, duration_min, price_eur, night, round_trip, map_url}.
    """
    payload = {
        "origin": origin,
        "destination": destination,
        "when": when,
        "round_trip": round_trip,
    }
    _validate_with_schema(payload, ESTIMATE_FARE_SCHEMA, where="estimate_fare.args")

    # Validate sub-objects as well (defense in depth)
    _validate_with_schema(origin, PLACE_OBJECT_SCHEMA, where="origin")
    _validate_with_schema(destination, PLACE_OBJECT_SCHEMA, where="destination")

    night = (when or "day") == "night"
    km_one_way = _haversine_km(origin["lat"], origin["lng"], destination["lat"], destination["lng"])
    total_km = km_one_way * (2.0 if round_trip else 1.0)

    per_km = NIGHT_KM if night else DAY_KM
    price = START_FEE + per_km * total_km

    # Urban-ish average speeds for more realistic durations
    avg_kmh = 26.0 if total_km < 8 else 35.0
    duration_min = int(round((total_km / max(avg_kmh, 1.0)) * 60))

    map_url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote_plus(origin.get('address') or origin.get('name', 'origin'))}"
        f"&destination={quote_plus(destination.get('address') or destination.get('name', 'destination'))}"
        "&travelmode=driving"
    )

    return {
        "distance_km": round(total_km, 2),
        "duration_min": duration_min,
        "price_eur": round(price, 2),
        "night": night,
        "round_trip": bool(round_trip),
        "map_url": map_url,
    }

# Attach metadata for fallback runtime
try:
    estimate_fare.__strict__ = True
    estimate_fare.__parameters__ = ESTIMATE_FARE_SCHEMA
except Exception:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# Rough distance + pricing helpers (legacy / fallback)

def _rough_distance_km(origin: str, destination: str) -> float:
    """
    Return a rough one-way distance between two locations based on a lookup table.
    If the origin/destination pair is unknown, return a default of 200km.
    """
    known = {
        ("πάτρα", "αθήνα"): 211.0,
        ("patra", "athens"): 211.0,
        ("πάτρα", "ιωάννινα"): 221.1,
        ("patra", "ioannina"): 221.1,
        ("πάτρα", "πρέβεζα"): 157.0,
        ("πάτρα", "καλαμάτα"): 210.0,
        ("πάτρα", "λουτράκι"): 184.0,
    }
    key = (origin.lower().strip(), destination.lower().strip())
    return float(known.get(key, 200.0))


def _estimate_price_and_time_km(
    distance_km: float, *, night: bool = False, round_trip: bool = False
) -> Dict[str, Any]:
    """Χονδρική εκτίμηση (χωρίς διόδια)."""
    per_km = NIGHT_KM if night else DAY_KM
    total_km = max(distance_km, 0.0) * (2.0 if round_trip else 1.0)
    cost = START_FEE + per_km * total_km
    avg_kmh = 83.0  # Assumed highway average speed (km/h) for long trips
    duration_h = total_km / max(avg_kmh, 1.0)
    duration_min = int(round(duration_h * 60))
    return {"distance_km": round(total_km, 1), "duration_min": duration_min, "price_eur": round(cost, 2)}


def _round5(eur: float) -> int:
    """Round a number to the nearest multiple of 5."""
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


def _is_round_trip(message: str) -> bool:
    t = (message or "").lower()
    return bool(re.search(r"(επιστροφ|πήγαινε[\s\-–]*έλα|πηγαιν[\s\-–]*ελα|round\s*trip|με\s+επιστροφ)", t))


def _extract_route_free_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (origin, destination) extracted from free text."""
    s = _preclean_route_text(text)
    if not s:
        return None, None

    m = re.search(r"\bαπό\s+(?P<o>.+?)\s+(?:μέχρι|έως|προς|για)\s+(?P<d>.+)$", s, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"^(?P<o>.+?)\s+(?:προς|για)\s+(?P<d>.+)$", s, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"^(?P<d>.+?)\s+από\s+(?P<o>.+)$", s, flags=re.IGNORECASE)

    if not m:
        # Fall back: capture queries that only specify a destination,
        # e.g. “μέχρι τη Θήβα” ή “για Θήβα” and assume a default origin.
        m2 = re.search(r"(?:μέχρι|έως|προς|για)\s+(?P<d>.+)$", s, flags=re.IGNORECASE)
        if m2:
            d2 = (m2.group("d") or "").strip(" ,.;·")
            # Ignore trivial destinations and stopwords
            if d2 and (d2.lower() not in _ROUTE_STOPWORDS) and len(d2) > 1:
                return DEFAULT_ORIGIN, d2
        return None, None

    o = (m.group("o") or "").strip(" ,.;·") if "o" in m.groupdict() else None
    d = (m.group("d") or "").strip(" ,.;·") if "d" in m.groupdict() else None

    if d and (d.lower() in _ROUTE_STOPWORDS or len(d) <= 1):
        d = None

    return (o or None), (d or None)


def _normalize_minutes(val: Any, distance_km: Optional[float] = None) -> Optional[int]:
    """Normalize various representations of duration to minutes."""
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
        approx = int(round((float(distance_km) / 83.0) * 60))
        return max(approx, 0)
    return None


def _fmt_minutes(mins: Optional[int]) -> Optional[str]:
    """Format minutes into a human-readable Greek string."""
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
    """
    Extract origin/destination from free text and estimate trip cost, distance,
    and duration. First attempts strict tools (resolve_place → estimate_fare).
    If that fails, uses the external Timologio API if available; otherwise
    provides a fallback estimate. Always includes a Google Maps link to the
    calculated route. The output is a human-friendly string.
    """
    logger.info("[tool] trip_quote_nlp parse")
    origin_txt, dest_txt = _extract_route_free_text(message)
    if not origin_txt or not dest_txt:
        return UI_TEXT.get(
            "ask_trip_route",
            "❓ Πες μου από πού ξεκινάς και πού πας (π.χ. «Από Πάτρα μέχρι Διακοπτό»).",
        )

    night_flag = _detect_night_or_double_tariff(message, when)
    round_trip_flag = _is_round_trip(message)

    # 0) STRICT PIPELINE (preferred)
    try:
        o = resolve_place(query=origin_txt)  # dict with place_id/lat/lng
        d = resolve_place(query=dest_txt)
        res = estimate_fare(
            origin=o,
            destination=d,
            when=("night" if night_flag else "day"),
            round_trip=round_trip_flag,
        )
        # Format nice reply
        parts: List[str] = []
        if round_trip_flag:
            parts.append(f"💶 Εκτίμηση: {_round5(res['price_eur'])}€ (πήγαινε–έλα)" + (" (νύχτα)" if night_flag else ""))
            parts.append(
                f"🛣️ Συνολική απόσταση: ~{res['distance_km']} km"
            )
        else:
            parts.append(f"💶 Εκτίμηση: {_round5(res['price_eur'])}€" + (" (νύχτα)" if night_flag else ""))
            parts.append(f"🛣️ Απόσταση: ~{res['distance_km']} km")
        dur_text = _fmt_minutes(res.get("duration_min"))
        if dur_text:
            parts.append(f"⏱️ Χρόνος: ~{dur_text}")
        if res.get("map_url"):
            parts.append(f"[📌 Δες τη διαδρομή στον χάρτη]({res['map_url']})")
        parts.append(UI_TEXT.get("fare_disclaimer", "⚠️ Η τιμή δεν περιλαμβάνει διόδια."))
        return "\n".join(parts)
    except Exception:
        logger.exception("strict tools pipeline failed; falling back")

    # 1) Timologio API path
    data: Dict[str, Any] = {"error": "unavailable"}
    if TimologioClient is not None:
        try:
            client = TimologioClient()
            data = client.estimate_trip(origin_txt, dest_txt, when=when)
            logger.debug("[tool] timologio ok: keys=%s", list(data.keys()))
        except Exception:
            logger.exception("[tool] timologio call failed")

    # 2) SUCCESS PATH using external API
    if isinstance(data, dict) and "error" not in data:
        dist = data.get("distance_km") or data.get("km") or data.get("distance")
        raw_dur = (
            data.get("duration_min")
            or data.get("minutes")
            or data.get("duration")
            or data.get("duration_seconds")
        )
        mins = _normalize_minutes(raw_dur, distance_km=dist)
        dur_text = _fmt_minutes(mins) if mins is not None else None
        map_url = data.get("map_url") or data.get("mapLink") or data.get("route_url") or data.get("map")

        # If distance is available, parse numeric value for fallback estimation
        km_val: Optional[float]
        if dist is not None:
            try:
                km_val = float(str(dist).replace("km", "").strip())
            except Exception:
                km_val = None
        else:
            km_val = None

        parts: List[str] = []

        if round_trip_flag and km_val is not None:
            est = _estimate_price_and_time_km(km_val, night=night_flag, round_trip=True)
            rounded = _round5(est["price_eur"])
            parts.append(f"💶 Εκτίμηση: {rounded}€ (πήγαινε–έλα)")
            parts.append(f"🛣️ Συνολική απόσταση: ~{est['distance_km']} km (2×{round(km_val, 1)} km)")
            parts.append(f"⏱️ Χρόνος: ~{_fmt_minutes(est['duration_min'])}")
        else:
            price = data.get("price_eur") or data.get("price") or data.get("total_eur") or data.get("fare")
            if price is None and km_val is not None:
                est = _estimate_price_and_time_km(km_val, night=night_flag, round_trip=False)
                price = est["price_eur"]
                if dur_text is None:
                    dur_text = _fmt_minutes(est["duration_min"])
            if price is not None:
                try:
                    price_val = float(str(price).replace(",", "."))
                    rounded = _round5(price_val)
                    parts.append(f"💶 Τιμή: {rounded}€")
                except Exception:
                    parts.append(f"💶 Τιμή: {price}€")
            if km_val is not None:
                parts.append(f"🛣️ Απόσταση: ~{round(float(km_val), 1)} km")
            if dur_text is not None:
                parts.append(f"⏱️ Χρόνος: ~{dur_text}")

        if not map_url:
            map_url = (
                f"https://www.google.com/maps/dir/?api=1&origin={quote_plus(origin_txt)}"
                f"&destination={quote_plus(dest_txt)}&travelmode=driving"
            )
        parts.append(f"[📌 Δες τη διαδρομή στον χάρτη]({map_url})")
        parts.append(UI_TEXT.get("fare_disclaimer", "⚠️ Η τιμή δεν περιλαμβάνει διόδια."))
        return "\n".join(parts)

    # 3) FALLBACK when Timologio is unavailable
    logger.warning("[tool] timologio unavailable, using fallback")
    one_way_km = _rough_distance_km(origin_txt, dest_txt)
    est = _estimate_price_and_time_km(one_way_km, night=night_flag, round_trip=round_trip_flag)
    dur_text = _fmt_minutes(est["duration_min"]) or "—"
    map_url = (
        f"https://www.google.com/maps/dir/?api=1&origin={quote_plus(origin_txt)}"
        f"&destination={quote_plus(dest_txt)}&travelmode=driving"
    )

    label = "Εκτίμηση"
    rt_flag = " (πήγαινε–έλα)" if round_trip_flag else ""
    body = [
        f"💶 {label}: {_round5(est['price_eur'])}€{rt_flag}" + (" (νύχτα)" if night_flag else ""),
        f"🛣️ {'Συνολική απόσταση' if round_trip_flag else 'Απόσταση'}: ~{est['distance_km']} km"
        + (f" (2×{round(one_way_km, 1)} km)" if round_trip_flag else ""),
        f"⏱️ Χρόνος: ~{dur_text}",
        f"[📌 Δες τη διαδρομή στον χάρτη]({map_url})",
        UI_TEXT.get("fare_disclaimer", "⚠️ Η τιμή δεν περιλαμβάνει διόδια."),
    ]
    return "\n".join(body)


@function_tool
def trip_estimate(origin: str, destination: str, when: str = "now") -> str:
    """Return a simple trip estimate for a given origin/destination."""
    try:
        dist = _rough_distance_km(origin, destination)
        night = _detect_night_or_double_tariff("", when)
        est = _estimate_price_and_time_km(dist, night=night, round_trip=False)
        map_url = (
            f"https://www.google.com/maps/dir/?api=1&origin={quote_plus(origin)}"
            f"&destination={quote_plus(destination)}&travelmode=driving"
        )
        return (
            f"💶 Εκτίμηση: {_round5(est['price_eur'])}€" + (" (νύχτα)" if night else "") + "\n"
            f"🛣️ Απόσταση: ~{est['distance_km']} km\n"
            f"⏱️ Χρόνος: ~{_fmt_minutes(est['duration_min'])}\n"
            f"[📌 Δες τη διαδρομή στον χάρτη]({map_url})\n"
            f"⚠️ Η τιμή δεν περιλαμβάνει διόδια."
        )
    except Exception:
        logger.exception("trip_estimate failed")
        return "❌ Δεν μπόρεσα να υπολογίσω την εκτίμηση."

# ──────────────────────────────────────────────────────────────────────────────
# Επαφές Taxi (από constants με fallback σε .env)

TAXI_EXPRESS_PHONE = _brand("phone", "TAXI_EXPRESS_PHONE") or "2610 450000"
TAXI_SITE_URL = _brand("site_url", "TAXI_SITE_URL") or "https://taxipatras.com"
TAXI_BOOKING_URL = _brand("booking_url", "TAXI_BOOKING_URL")  # no insecure fallback
TAXI_APP_URL = _brand("app_url", "TAXI_APP_URL")  # optional; no insecure fallback


@function_tool
def taxi_contact(city: str = "Πάτρα") -> str:
    """Return contact information for taxi services in Patras."""
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
    """Construct regex patterns for mapping area aliases to canonical names."""
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
    """Extract a canonical area from user text based on defined aliases."""
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
    """Return a trendy phrase for the given parameters, or an empty string if unavailable."""
    t = pick_trendy_phrase(emotion=emotion, context=context, lang=lang, season=season)
    return t or ""


@function_tool
def pharmacy_lookup(area: str = DEFAULT_AREA, method: str = "get") -> str:
    """Return a list of on-duty pharmacies for a given area."""
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
    """
    Extract the area from a message and return on-duty pharmacies. If no area
    is found, prompt the user to specify one. Does not attempt to set
    session state; the caller should manage conversational context.
    """
    if PharmacyClient is None:
        return "❌ PharmacyClient δεν είναι διαθέσιμος."

    area = _area_from_text(message)
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
    reply = "\n".join(lines).strip()
    # Prepend a trendy phrase for a friendly tone
    try:
        phrase = trendy_phrase(emotion="joy", context="pharmacy", lang="el")
    except Exception:
        phrase = ""
    if phrase:
        reply = f"💬 {phrase}\n\n{reply}"
    return reply

# ──────────────────────────────────────────────────────────────────────────────
# Νοσοκομεία / Γενικές Πάτρας

@function_tool
def hospital_duty(which_day: str = "σήμερα") -> str:
    """Return on-duty hospitals for the given day. A trendy phrase is prepended for a friendly tone."""
    if HospitalsClient is None:
        return "❌ HospitalsClient δεν είναι διαθέσιμος."
    client = HospitalsClient()
    try:
        result = client.which_hospital(which_day=which_day)
        # Prepend a trendy phrase for a friendly tone
        try:
            phrase = trendy_phrase(emotion="joy", context="hospital", lang="el")
        except Exception:
            phrase = ""
        if phrase:
            result = f"💬 {phrase}\n\n{result}"
        return result
    except Exception:
        logger.exception("hospital_duty failed")
        return UI_TEXT.get("generic_error", "❌ Δεν κατάφερα να φέρω τα εφημερεύοντα νοσοκομεία.")


@function_tool
def patras_info(query: str) -> str:
    """Return information about Patras based on a user query."""
    if PatrasAnswersClient is None:
        return "❌ PatrasAnswersClient δεν είναι διαθέσιμος."
    client = PatrasAnswersClient()
    try:
        return client.ask(query)
    except Exception:
        logger.exception("patras_info failed")
        return UI_TEXT.get("generic_error", "❌ Δεν κατάφερα να βρω πληροφορίες.")


def detect_area_for_pharmacy(message: str):
    """Detect area for pharmacy queries; fallback returns None."""
    try:
        return _area_from_text(message)
    except Exception:
        return None


def notify_booking_slack(payload: dict) -> bool:
    """Send booking notification to Slack via a webhook URL."""
    url = os.getenv("SLACK_BOOKING_WEBHOOK_URL")
    if not url:
        return False
    text = (
        "*ΝΕΑ ΚΡΑΤΗΣΗ*\n"
        f"Κωδικός: {payload.get('code','-')}\n"
        f"Από: {payload.get('origin','-')}\n"
        f"Προς: {payload.get('destination','-')}\n"
        f"Ώρα: {payload.get('pickup_time','-')}\n"
        f"Όνομα: {payload.get('name','-')}\n"
        f"Τηλ.: {payload.get('phone','-')}\n"
        f"Άτομα: {payload.get('pax','-')}\n"
        f"Αποσκευές: {payload.get('luggage_count','0')} (βαριές: {payload.get('luggage_heavy','όχι')})\n"
        f"Σχόλια: {payload.get('notes','-')}\n"
    )
    try:
        r = requests.post(url, json={"text": text}, timeout=5)
        return r.status_code < 300
    except Exception:
        return False

# ──────────────────────────────────────────────────────────────────────────────
# Make decorated tools callable when imported directly
# When the Agents SDK is available, @function_tool may wrap functions into
# objects that aren't directly callable. Keep old behavior by exposing __call__.
try:
    for _name in (
        "pharmacy_lookup",
        "pharmacy_lookup_nlp",
        "hospital_duty",
        "resolve_place",
        "estimate_fare",
    ):
        _obj = globals().get(_name)
        if _obj is not None and not callable(_obj):
            # Try common attribute names that hold the original function
            underlying = getattr(_obj, "func", None) or getattr(_obj, "_func", None) or getattr(_obj, "fn", None)
            if underlying and callable(underlying):
                def _make_call(u):
                    return lambda *a, **kw: u(*a, **kw)
                setattr(_obj, "__call__", _make_call(underlying))  # type: ignore
except Exception:
    pass
