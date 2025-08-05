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

# 3) Χάρτης canonical ονομάτων πόλεων/περιοχών (ΣΥΜΠΛΗΡΩΣΕ όσα χρειάζεσαι)
CITY_ALIAS: Dict[str, str] = {
    "πατρα": "Πάτρα",
    "πατρας": "Πάτρα",
    "παραλια": "Παραλία Πατρών",
    "ριον": "Ρίο",
    "rio": "Ρίο",
    "αθηνα": "Αθήνα",
    "αθηνας": "Αθήνα",
    "πυργος": "Πύργος",
    "πειραιας": "Πειραιάς",
    "ιτεα": "Ιτέα",
    "καλαματα": "Καλαμάτα",
    "ναυπλιο": "Ναύπλιο",
    "ζακυνθος": "Ζάκυνθος",
    "ανδραβιδα": "Ανδραβίδα",
    "αμαλιαδα": "Αμαλιάδα",
    # …
}

def normalize_city(raw: str) -> str:
    """
    Μετατρέπει ελεύθερο κείμενο πόλης σε canonical:
    π.χ. 'αθηναι' → 'Αθήνα', με fuzzy match.
    """
    key = normalize_text(raw)
    # απευθείας match
    if key in CITY_ALIAS:
        return CITY_ALIAS[key]
    # fuzzy match
    match = get_close_matches(key, list(CITY_ALIAS.keys()), n=1, cutoff=0.7)
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
    """
    Βελτιωμένο extraction για intents ταξιδιού (TripCostIntent) και γενικά για προορισμούς.
    Υποστηρίζει:
      - 'απο Πάτρα μεχρι Αθήνα'
      - 'να πάω αθήνα'
      - 'για ριον'
      - 'πάτρα αθήνα'
      - 'αθήνα'
    """
    text_norm = normalize_text(text)
    entities = {}

    # Πιάσε pattern "απο ... μεχρι/προς/για/εως ..."
    match = re.search(r"απο\s+(?P<from>\w+).*?(?:μεχρι|προς|για|εως)\s+(?P<to>\w+)", text_norm)
    if match:
        entities["FROM"] = normalize_city(strip_article(match.group("from")))
        entities["TO"] = normalize_city(strip_article(match.group("to")))
        return entities

    # Πιάσε "στην/στο/για/προς ..." (μόνο TO)
    loc_match = re.search(r"(?:στην|στη|στο|για|προς)\s+(?P<to>\w+)", text_norm)
    if loc_match:
        entities["FROM"] = "Πάτρα"
        entities["TO"] = normalize_city(strip_article(loc_match.group("to")))
        return entities

    # Πιάσε απλά δύο πόλεις στο κείμενο (χωρίς άρθρα/προθέσεις)
    words = [w for w in text_norm.split() if w.isalpha()]
    found_cities = [normalize_city(strip_article(w)) for w in words if normalize_city(w) != w.title()]
    if len(found_cities) == 2:
        entities["FROM"], entities["TO"] = found_cities
        return entities

    # Αν γράψει μόνο μια λέξη και είναι πόλη, τη θεωρούμε TO με default FROM
    if len(words) == 1 and words[0].isalpha():
        entities["FROM"] = "Πάτρα"
        entities["TO"] = normalize_city(strip_article(words[0]))
        return entities

    # Συμπληρωματικά: catch κάποιες οργανώσεις/ορολογίες (π.χ. νοσοκομείο, ταξί)
    if "κτελ" in text_norm:
        entities["organization"] = "ΚΤΕΛ"
    elif "ταξι" in text_norm or "taxi" in text_norm:
        entities["organization"] = "Taxi"
    elif "νοσοκομειο" in text_norm or "αγιοσ ανδρεασ" in text_norm:
        entities["organization"] = "Hospital"

    return entities
