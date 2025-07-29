import spacy
from spacy.training.example import Example
from spacy.util import minibatch
from spacy.lookups import Lookups  # 🔥 add this

# 1. Load blank model for Greek
nlp = spacy.blank("el")

# 2. Load lookups manually (required!)
lookups = Lookups()
nlp.vocab.lookups = lookups

# 3. Add NER pipe
ner = nlp.add_pipe("ner")

# 4. Define entities and training examples
TRAIN_DATA = [
    ("Ποιο είναι το τηλέφωνο του ΚΤΕΛ Αχαΐας;", {"entities": [(26, 37, "ORGANIZATION")]}),
    ("Πες μου για την ιστορία της Πάτρας", {"entities": [(26, 32, "LOCATION")]}),
    ("Θέλω πληροφορίες για το νοσοκομείο Άγιος Ανδρέας", {"entities": [(27, 44, "HOSPITAL")]}),
    ("Πόσο κοστίζει το ταξί για Ναύπλιο;", {"entities": [(27, 34, "DESTINATION")]}),
]

# 5. Add labels
for _, annotations in TRAIN_DATA:
    for ent in annotations.get("entities"):
        ner.add_label(ent[2])

# 6. Initialize model
nlp.initialize()

# 7. Training loop
for i in range(20):
    losses = {}
    batches = minibatch(TRAIN_DATA, size=2)
    for batch in batches:
        for text, annotations in batch:
            example = Example.from_dict(nlp.make_doc(text), annotations)
            nlp.update([example], losses=losses)
    print(f"Losses at iteration {i}: {losses}")

# 8. Save model
nlp.to_disk("trained_ner_el")
print("✅ Model saved to trained_ner_el/")
