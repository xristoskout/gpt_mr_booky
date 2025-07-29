import json
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional
from nlp_utils import extract_entities, normalize_text

logger = logging.getLogger(__name__)

INTENTS_FILE = Path("intents.json")


class IntentConfig:
    def __init__(self, data: Dict) -> None:
        self.examples = data.get("examples", [])


class IntentClassifier:
    def __init__(self, fuzzy_threshold: float = 0.8) -> None:
        self.fuzzy_threshold = fuzzy_threshold
        self.intents: Dict[str, IntentConfig] = {}

        # ✅ Φορτώνει το intents.json σωστά
        if not INTENTS_FILE.exists():
            logger.error(f"❌ Το αρχείο {INTENTS_FILE} δεν βρέθηκε.")
            raise FileNotFoundError(f"{INTENTS_FILE} not found")

        with open(INTENTS_FILE, "r", encoding="utf-8") as f:
            intent_data = json.load(f)
            self.intents = {
                name: IntentConfig(cfg) for name, cfg in intent_data.items()
            }

        logger.info(f"✅ Loaded {len(self.intents)} intents")

    def keyword_boosted(self, message: str) -> Optional[str]:
        nm = normalize_text(message)

        if any(k in nm for k in ("πατρα", "πατρας", "κτελ", "οσε", "σταθμος", "τηλεφωνο", "υπηρεσιες")):
            return "PatrasInfoIntent"

        if any(k in nm for k in ("φαρμακει", "διανυκτερευ", "εφημερευ", "εφημερεια")):
            return "OnDutyPharmacyIntent"

        if any(k in nm for k in ("νοσοκομει", "εφημερε", "εφημερευον")):
            return "HospitalIntent"

        if any(k in nm for k in ("ποσο", "κοστος", "χρεωση", "τιμη", "χιλ", "χιλιομετρα", "μεχρι", "απο", "εως", "ως", "διαδρομη", "αποσταση", "δρομολογιο", "ποια ειναι η αποσταση")):
            return "TripCostIntent"

        if any(k in nm for k in ("κρατηση", "booking", "τηλεφωνο", "email", "επικοινωνια", "εφαρμογη")):
            return "ContactInfoIntent"

        if any(k in nm for k in ("εκδρομη", "τουρ", "προορισμος", "ναυπακτος", "πακετο", "οδηγος")):
            return "ServicesAndToursIntent"

        return None

    def fuzzy_intent(self, message: str) -> str:
        nm = normalize_text(message)
        best_score, best_intent = 0.0, "default"

        for intent_name, cfg in self.intents.items():
            for example in cfg.examples:
                score = SequenceMatcher(None, normalize_text(example), nm).ratio()
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
        return {"intent": intent, "entities": entities}
