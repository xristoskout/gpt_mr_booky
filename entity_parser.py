import spacy
from typing import Dict
from nlp_utils import normalize_city, strip_article, extract_entities as rule_based_extract

# Αν έχεις ήδη φορτωμένο το μοντέλο, βάλε try/catch ή κάνε το load στο startup.
try:
    nlp = spacy.load("trained_ner_el")
except Exception:
    nlp = spacy.blank("el")  # fallback, δε θα δουλέψει NER, αλλά δεν θα σπάει ο agent

def extract_entities(text: str, fallback_rule_based: bool = True) -> Dict[str, str]:
    """
    Extract entities from Greek text using spaCy trained model.
    Tries to map NER labels to business logic (FROM, TO, destination).
    If spaCy fails, uses rule-based as fallback (if enabled).
    """
    doc = nlp(text)
    entities = {}

    # Mapping, adjust as per your NER labels (π.χ. GPE, LOC → TO, FROM, κλπ)
    label_map = {
        "FROM": "FROM",
        "TO": "TO",
        "LOC": "TO",
        "GPE": "TO",
        "DEST": "TO",
        "ORG": "organization",
        "CITY": "TO",
    }

    for ent in doc.ents:
        label = label_map.get(ent.label_, ent.label_)
        val = normalize_city(strip_article(ent.text))
        if label not in entities:
            entities[label] = val
        else:
            # Αν υπάρχει ήδη, βάλε ως λίστα (πχ αν βρίσκει πολλά TO)
            if isinstance(entities[label], list):
                entities[label].append(val)
            else:
                entities[label] = [entities[label], val]

    print(f"[EntityParser] Text: {text} → Entities: {entities}")

    # Fallback σε rule-based extraction αν δεν βρήκε βασικά entities (π.χ. TO)
    if fallback_rule_based and ("TO" not in entities or not entities["TO"]):
        rb = rule_based_extract(text)
        for k, v in rb.items():
            if k not in entities or not entities[k]:
                entities[k] = v

    return entities
