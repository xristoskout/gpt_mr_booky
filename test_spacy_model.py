import spacy
from pathlib import Path

# Βεβαιώσου ότι ο φάκελος υπάρχει
model_path = Path("trained_ner_el")
if not model_path.exists():
    raise Exception("❌ Το trained_ner_el δεν βρέθηκε στον φάκελο!")

# Φόρτωση μοντέλου
nlp = spacy.load(model_path)
print("✅ Το μοντέλο φορτώθηκε επιτυχώς!")

# Δείξε labels από NER
if "ner" in nlp.pipe_names:
    ner = nlp.get_pipe("ner")
    print("📦 Entities που αναγνωρίζει:")
    for label in ner.labels:
        print(f"  - {label}")
else:
    print("⚠ Δεν υπάρχει 'ner' pipe στο μοντέλο.")

# Δοκιμαστικό input
text = "Θέλω να πάω στην Πάτρα και μετά στο ΚΤΕΛ Αχαΐας."
doc = nlp(text)
print("\n🔍 Αποτελέσματα entity extraction:")
for ent in doc.ents:
    print(f"  → [{ent.label_}] {ent.text}")
