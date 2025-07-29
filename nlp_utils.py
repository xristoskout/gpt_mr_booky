# nlp_utils.py

import re
import unicodedata
from difflib import SequenceMatcher, get_close_matches
from typing import Dict, Optional

# 1) Αφαίρεση τόνων
def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )

# 2) Κανονικοποίηση κειμένου (lowercase, χωρίς τόνους, ς→σ, strip whitespace)
def normalize_text(text: str) -> str:
    t = strip_accents(text)
    return t.lower().replace("ς", "σ").strip()

# 3) Χάρτης canonical ονομάτων πόλεων/περιοχών
CITY_ALIAS: Dict[str, str] = {
    "πατρα": "Πάτραι",
    "πατρας": "Πτρα",
    "παραλια": "Παραλία Πατρών",
    "ριον": "Ρίο",
    "rio": "Ρίο",
    "αθηνα": "Αθήνα",
    "πυργος": "Πύργος",
    "πειραια": "Περαια",
    "ιτεα": "Ιτέα",
    # … συμπλήρωσε όσα χρειάζεσαι …
}

def normalize_city(raw: str) -> str:
    """
    Μετατρέπει ελεύθερο κείμενο πόλης σε canonical:
    π.χ. 'αθηναι' → 'Αθήνα', 'πτρα' → 'Πάτρα', με fuzzy match.
    """
    key = normalize_text(raw)
    # απευθείας match
    if key in CITY_ALIAS:
        return CITY_ALIAS[key]
    # fuzzy match
    match = get_close_matches(key, list(CITY_ALIAS.keys()), n=1, cutoff=0.65)
    if match:
        return CITY_ALIAS[match[0]]
    # fallback: απλώς τίτλος
    return raw.title()

# 4) Αφαίρεση άρθρων
ARTICLE_RE = re.compile(r"^(?:τον|την|τη|το|ο|η)\s+", re.IGNORECASE)
def strip_article(text: str) -> str:
    return ARTICLE_RE.sub("", text).strip()

# 5) Canonical aliases για περιοχές (αν θέλεις ξεχωριστά από πόλεις)
RAW_AREA_ALIASES = {
    "πατρα": "Πάτρα",
    "πατρας": "Πάτρα",
    "παραλια": "Παραλία Πατρών",
    "ριον": "Ρίο",
    "rio": "Ρίο",
    # …
}
AREA_ALIASES: Dict[str, str] = {
    normalize_text(k): v for k, v in RAW_AREA_ALIASES.items()
}

def extract_area(token: str) -> Optional[str]:
    nm = normalize_text(token)
    # ακριβές match
    if nm in AREA_ALIASES:
        return AREA_ALIASES[nm]
    # partial
    for alias, canon in AREA_ALIASES.items():
        if alias in nm or nm in alias:
            return canon
    # fuzzy
    best_score, best_area = 0.0, None
    for alias, canon in AREA_ALIASES.items():
        score = SequenceMatcher(None, alias, nm).ratio()
        if score > best_score:
            best_score, best_area = score, canon
    return best_area if best_score >= 0.5 else None
    
def extract_entities(text: str) -> dict:
    # Normalize
    text = text.lower().strip()

    entities = {}

    # Rule-based entity extraction (can upgrade with spaCy/el/NER)
    if "κτελ" in text:
        entities["organization"] = "ΚΤΕΛ"
    elif "ταξί" in text or "taxi" in text:
        entities["organization"] = "Taxi"
    elif "νοσοκομείο" in text or "αγιος ανδρεας" in text:
        entities["organization"] = "Hospital"

    # Location based
    destinations = ["πάτρα", "καλαμάτα", "ναύπλιο", "αμαλιάδα", "αθήνα", "ζάκυνθος", "πύργος", "ανδραβίδα"]
    for city in destinations:
        if city in text:
            entities["destination"] = city.capitalize()

    return entities