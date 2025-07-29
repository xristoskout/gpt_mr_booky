import spacy

nlp = spacy.load("trained_ner_el")

def extract_entities(text: str):
    doc = nlp(text)
    entities = {}
    for ent in doc.ents:
        if ent.label_ not in entities:
            entities[ent.label_] = [ent.text]
        else:
            entities[ent.label_].append(ent.text)
    print(f"[EntityParser] Text: {text} → Entities: {entities}")
    return entities
