import json
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

INTENTS_FILE = Path("intents.json")

# Λίστα περιοχών για την αναγνώριση (μπορείς να προσθέσεις περισσότερες)
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

def extract_entities(text: str) -> Dict[str, str]:
    entities = {}
    area = extract_area(text)
    if area:
        entities["AREA"] = area
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

    def keyword_boosted(self, message: str) -> Optional[str]:
        nm = message.lower()

        # TripCostIntent πρέπει να προηγείται!
        if any(k in nm for k in ("ποσο", "κοστος", "χρεωση", "τιμη", "χιλ", "χιλιομετρα", "χλμ", "μεχρι", "απο", "εως", "ως", "διαδρομη", "αποσταση", "δρομολογιο")):
            return "TripCostIntent"

        if any(k in nm for k in ("νοσοκομει","νοσοκομειο","νοσοκομεια", "κλινικ", "εφημερ", "εφημερευον")):
            return "HospitalIntent"

        if any(k in nm for k in ("φαρμακει","φαρμακ", "διανυκτερευ", "εφημερ", "εφημερεια")):
            return "OnDutyPharmacyIntent"

        if any(k in nm for k in ("πατρα", "πατρας", "κτελ", "οσε", "σταθμος","φαμε", "φαγητο", "εστιατορ", "μπαν","καφε", "μπάνι", "παραλι", "θαλασσ", "τηλεφωνο", "υπηρεσιες")):
            return "PatrasLlmAnswersIntent"

        if any(k in nm for k in ("κρατηση", "booking", "τηλεφωνο", "email", "επικοινωνια", "εφαρμογη", "app")):
            return "ContactInfoIntent"

        if any(k in nm for k in ("εκδρομη", "τουρ", "προορισμος", "ναυπακτος", "ολυμπια","πακετο", "οδηγος", "εκδρομες", "κανετε εκδρομες", "πακετα")):
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

    def detect(self, message: str) -> Dict[str, str]:
        intent = self.keyword_boosted(message)
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
