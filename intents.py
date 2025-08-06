import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)

INTENTS_FILE = Path("intents.json")

# ---- Περιοχές για φαρμακείο, προσάρμοσέ το αν θες ----
AREAS = [
    "Πάτρα", "Παραλία Πατρών", "Βραχνέικα", "Ρίο", "Μεσσάτιδα"
]

def extract_area(text: str) -> Optional[str]:
    text_lower = text.lower()
    for area in AREAS:
        area_variants = [area.lower()]
        # Προσθήκη παραλλαγών για αποφυγή τόνων ή συντομεύσεων
        if area == "Βραχνέικα":
            area_variants += ["βραχνεϊκα", "βραχνεικα"]
        elif area == "Παραλία Πατρών":
            area_variants += ["παραλία", "παραλια"]
        elif area == "Μεσσάτιδα":
            area_variants += ["μεσάτιδα", "μεσσατιδα"]
        elif area == "Ρίο":
            area_variants += ["ριο"]
        for variant in area_variants:
            if variant in text_lower:
                 return area
    return None

# ---- ΒΑΣΙΚΗ & ΕΥΦΥΗΣ ΑΝΑΓΝΩΡΙΣΗ entities ----
def extract_entities(text: str, context_slot=None):
    text = text.strip().lower()
    entities = {}

    # 1. "από XXX για YYY" ή "από XXX μέχρι YYY"
    m = re.search(
        r"απ[οό]\s+([\wάέίήύόώϊϋΐΰ\s\-]+)\s+(?:μέχρι|μεχρι|για|προς|έως|εως|ως)\s+([\wάέίήύόώϊϋΐΰ\s\-]+)",
        text,
    )
    if m:
        entities["FROM"] = m.group(1).strip().title()
        entities["TO"] = m.group(2).strip().title()
        return entities

    # 2. "XXX YYY" (δυο λέξεις, π.χ. "πάτρα αθήνα")
    m = re.match(r"^([\wάέίήύόώϊϋΐΰ\-]+)\s+([\wάέίήύόώϊϋΐΰ\-]+)$", text)
    if m:
        entities["FROM"] = m.group(1).strip().title()
        entities["TO"] = m.group(2).strip().title()
        return entities

    # 3. "για YYY" / "μέχρι YYY"
    m = re.search(
        r"(?:για|μέχρι|μεχρι|προς|έως|εως|ως)\s+([\wάέίήύόώϊϋΐΰ\s\-]+)",
        text,
    )
    if m:
        entities["TO"] = m.group(1).strip().title()
        if context_slot == "FROM":
            entities["FROM"] = None
        return entities

    # 4. "από XXX"
    m = re.search(r"απ[οό]\s+([\wάέίήύόώϊϋΐΰ\s\-]+)", text)
    if m:
        entities["FROM"] = m.group(1).strip().title()
        if context_slot == "TO":
            entities["TO"] = None
        return entities

    # 5. Φαρμακείο/νοσοκομείο + περιοχή
    m = re.search(
        r"(φαρμακειο|φαρμακείο|νοσοκομ[ειοίαή])\s+([\wάέίήύόώϊϋΐΰ\s\-]+)", text
    )
    if m:
        entities["area"] = m.group(2).strip().title()
        return entities

    # 6. Πιάσε φαρμακείο περιοχή από λίστα
    area = extract_area(text)
    if area:
        entities["area"] = area

    # 7. Μια λέξη (μόνο προορισμός ή περιοχή)
    words = text.split()
    if len(words) == 1 and 2 < len(words[0]) < 24:
        if context_slot == "TO":
            entities["TO"] = words[0].title()
        elif context_slot == "FROM":
            entities["FROM"] = words[0].title()
        else:
            entities["TO"] = words[0].title()

    return entities


class IntentConfig:
    def __init__(self, data: Dict) -> None:
        self.examples = data.get("examples", [])


class IntentClassifier:
    def __init__(self, intents_path: Path = INTENTS_FILE, fuzzy_threshold: float = 0.8) -> None:
        self.fuzzy_threshold = fuzzy_threshold
        self.intents: Dict[str, IntentConfig] = {}

        path = Path(intents_path)
        if not path.exists():
            logger.error(f"❌ Το αρχείο {path} δεν βρέθηκε.")
            raise FileNotFoundError(f"{path} not found")

        with open(path, "r", encoding="utf-8") as f:
            intent_data = json.load(f)
            self.intents = {name: IntentConfig(cfg) for name, cfg in intent_data.items()}

        logger.info(f"✅ Loaded {len(self.intents)} intents")

    def keyword_boosted(
        self,
        message: str,
        active_intent: Optional[str] = None,
        missing_slots: Optional[List[str]] = None,
    ) -> Optional[str]:
        nm = message.lower()

        # TripCostIntent πρέπει να προηγείται!
        if any(
            k in nm
            for k in (
                "ποσο",
                "κοστος",
                "χρεωση",
                "τιμη",
                "χιλ",
                "χιλιομετρα",
                "χλμ",
                "μεχρι",
                "απο",
                "εως",
                "ως",
                "διαδρομη",
                "αποσταση",
                "δρομολογιο",
            )
        ):
            return "TripCostIntent"

        if any(
            k in nm
            for k in ("νοσοκομει", "νοσοκομειο", "νοσοκομεια", "κλινικ")
        ):
            return "HospitalIntent"

        if any(k in nm for k in ("φαρμακει", "φαρμακ", "διανυκτερευ")):
            return "OnDutyPharmacyIntent"

        if any(
            k in nm
            for k in (
                "πατρα",
                "πατρας",
                "κτελ",
                "οσε",
                "σταθμος",
                "φαμε",
                "φαγητο",
                "εστιατορ",
                "μπαν",
                "καφε",
                "μπάνι",
                "παραλι",
                "θαλασσ",
                "τηλεφωνο",
                "υπηρεσιες",
            )
        ):
            if not (active_intent and missing_slots):
                return "PatrasLlmAnswersIntent"

        if any(
            k in nm
            for k in (
                "κρατηση",
                "booking",
                "τηλεφωνο",
                "email",
                "επικοινωνια",
                "εφαρμογη",
                "app",
            )
        ):
            return "ContactInfoIntent"

        if any(
            k in nm
            for k in (
                "εκδρομη",
                "τουρ",
                "προορισμος",
                "ναυπακτος",
                "ολυμπια",
                "πακετο",
                "οδηγος",
                "εκδρομες",
                "κανετε εκδρομες",
                "πακετα",
            )
        ):
            return "ServicesAndToursIntent"

        return None

    def fuzzy_intent(self, message: str) -> str:
        nm = message.lower()
        best_score, best_intent = 0.0, "default"

        for intent_name, cfg in self.intents.items():
            for example in cfg.examples:
                score = SequenceMatcher(None, example.lower(), nm).ratio()
                if score > best_score:
                    best_score = score
                    best_intent = intent_name

        logger.debug(f"Fuzzy match: {best_intent} ({best_score:.2f})")
        return best_intent if best_score >= self.fuzzy_threshold else "default"

    def detect(
        self,
        message: str,
        active_intent: Optional[str] = None,
        missing_slots: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        intent = self.keyword_boosted(message, active_intent, missing_slots)
        if intent:
            logger.info(f"📌 Boosted intent: {intent}")
        else:
            intent = self.fuzzy_intent(message)

        entities = extract_entities(message)
        # Πιάσε και single word ως TO για ταξί
        if intent == "TripCostIntent" and not entities.get("TO"):
            if len(message.strip().split()) == 1 and message.strip().isalpha():
                entities = {"FROM": "Πάτρα", "TO": message.strip().capitalize()}
        logger.info(f"[INTENT]: {intent}, [ENTITIES]: {entities} για input: '{message}'")
        return {"intent": intent, "entities": entities}