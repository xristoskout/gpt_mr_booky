import os
import json
import string
import requests
import unicodedata
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv
from difflib import SequenceMatcher

# --- Environment & OpenAI Client ---
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=api_key)

# --- Flask App ---
app = Flask(__name__)
CORS(app)

# --- External API URLs ---
PHARMACY_API_URL   = "https://pharmacy-api-160866660933.europe-west1.run.app/pharmacy"
DISTANCE_API_URL   = "https://distance-api-160866660933.europe-west1.run.app/calculate_route_and_fare"
HOSPITAL_API_URL   = "https://patra-hospitals-webhook-160866660933.europe-west1.run.app/"
TIMOLOGIO_API_URL  = "https://timologio-160866660933.europe-west1.run.app/calculate_fare"

# --- In-memory Sessions ---
SESSIONS = {}

# --- System Prompt for fallback GPT ---
SYSTEM_PROMPT = """
Είσαι ο Mr Booky, ο ψηφιακός βοηθός του Taxi Express Πάτρας (https://taxipatras.com).
- Άμεση εξυπηρέτηση 24/7 – 365 ημέρες το χρόνο
- Στόλος 160 οχημάτων, έμπειροι οδηγοί
- Τηλέφωνο: 2610 450000, Booking: https://booking.infoxoros.com/?key=cbe08ae5-d968-43d6-acba-5a7c441490d7
- Υπηρεσίες: Εκδρομές, τουριστικές μετακινήσεις, σχολικά, Night Taxi, εταιρικές μεταφορές, Courier, μεταφορά κατοικιδίων, παιδιών κ.ά.
- Για άμεση κράτηση, ενημέρωσε τον χρήστη για τα παραπάνω.

🔔 ΤΙΜΟΛΟΓΙΟ ΤΑΞΙ ΠΑΤΡΑΣ
- Ελάχιστη αποζημίωση: 4.00€
- Εκκίνηση/Σημαία: Περιλαμβάνεται στην ελάχιστη.
- Τιμή ανά χλμ. (εντός Πάτρας/ζώνη 1): 0.90€/χλμ
- Τιμή ανά χλμ. (εκτός Πάτρας/ζώνη 2 ή βράδυ): 1.25€/χλμ (διπλή ταρίφα)
- Ραδιοταξί απλή κλήση: 1.92€ (άρα ελάχιστη με ραδιοταξί = 5.92€)
- Ραντεβού ραδιοταξί: 3.39€ έως 5.65€
- Αποσκευές >10kg: 0.39€/τεμάχιο
- Από/προς αεροδρόμιο: +4.00€
- Από/προς σταθμό: +1.07€
- Χρέωση αναμονής: 15€/ώρα

Διευκρίνιση: Διόδια & ferry πληρώνονται έξτρα από τον πελάτη.

Όταν απαντάς για διαδρομές εκτός Πάτρας ή Αθήνα, **πάντα αναφέρεις ότι στην τιμή δεν περιλαμβάνονται τα διόδια**.

Αν δεν καταλαβαίνεις, δίνεις επιλογές/κατηγορίες. Κλείνεις πάντα: "Ευχαριστώ πολύ που επικοινώνησες μαζί μας! Αν χρειαστείς κάτι άλλο, είμαστε εδώ."
"""

# --- Load Intents ---
def load_intents():
    with open("intents.json", encoding="utf-8") as f:
        return json.load(f)
INTENTS = load_intents()

# --- Text Utilities ---
def strip_accents(text: str) -> str:
    return ''.join(ch for ch in unicodedata.normalize('NFD', text)
                   if unicodedata.category(ch) != 'Mn')

def normalize_text(text: str) -> str:
    t = strip_accents(text.lower())
    return t.translate(str.maketrans('', '', string.punctuation))

# --- Area Aliases for Pharmacy (only in bot) ---
AREA_ALIASES = {
    "ριο":             "Ρίο",
    "ριον":            "Ρίο",
    "πατρα":           "Πτρα",
    "πατρας":          "Πάτρα",
    "παραλια πατρων":  "Παραλία Πατρα",
    "παραλια":         "Παραλία Πατρών",
    "οβρυα":           "Οβριά",
    "μεσατιδα":        "Οβρυά",
    "μεσσατιδα":       "Οβρυά",
    "βραχνεικα":       "Βραχν",
    "rio":             "Ρίο",
    "πατραι":          "Πτρα",
    "πατρας":          "Πάτρα",
    "παραλια πατρων":  "Παραλ",
    "παραλια":         "Παραλία Πατρών",
    "οβρυα":           "Οβριά",
    "μεσατιδα":        "obria",
    "μεσσατιδα":       "Οβρυά",
    "βραχνεικα":       "Βραχν",
}

def extract_area(message: str) -> str | None:
    msg = normalize_text(message)
    for alias, canon in AREA_ALIASES.items():
        if alias in msg:
            return canon
    return None

# --- Intent Classification ---
def fuzzy_match(message: str) -> str:
    best_score, best_intent = 0.0, "default"
    msg_norm = normalize_text(message)
    for intent, data in INTENTS.items():
        for ex in data.get("examples", []):
            score = SequenceMatcher(None, normalize_text(ex), msg_norm).ratio()
            if score > best_score:
                best_score, best_intent = score, intent
    return best_intent if best_score > 0.8 else "default"

def gpt_intent_classifier(message: str) -> str:
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content":
                    "Κατάταξε το μήνυμα σε ένα από τα intents: "
                    "[OnDutyPharmacyIntent, HospitalIntent, ContactInfoIntent, "
                    "PricingInfoIntent, ServicesAndToursIntent, TripCostIntent, EndConversationIntent]. "
                    "Επέστρεψε μόνο το όνομα."},
                {"role": "user", "content": message}
            ]
        )
        return resp.choices[0].message.content.strip()
    except:
        return "default"

def keyword_boosted_intent(message: str) -> str | None:
    msg = normalize_text(message)
    if "φαρμακει" in msg:
        return "OnDutyPharmacyIntent"
    if "νοσοκομει" in msg:
        return "HospitalIntent"
    if "αποσκευ" in msg or "βαλιτσ" in msg:
        return "PricingInfoIntent"
    if any(kw in msg for kw in ("ποσα χιλιομετρα","κοστος διαδρομης","μεχρι")):
        return "TripCostIntent"
    if any(bye in msg for bye in ("ευχαριστ","αντιο","bye","goodbye")):
        return "EndConversationIntent"
    if any(srv in msg for srv in ("εκδρομ","τουριστ","courier","night","σχολ")):
        # γενικό hint για υπηρεσίες
        return "ServicesAndToursIntent"
    return None

def detect_intent(message: str) -> str:
    boosted = keyword_boosted_intent(message)
    if boosted:
        return boosted
    intent = fuzzy_match(message)
    if intent != "default":
        return intent
    return gpt_intent_classifier(message)

# --- Trip Cost Extraction ---
def extract_destination(message: str) -> str | None:
    words = normalize_text(message).split()
    for w in reversed(words):
        if w not in {"απο","μεχρι","ως","για","εως"}:
            return w.title()
    return None

# --- API Calls ---
def get_on_duty_pharmacies(area: str) -> dict:
    try:
        app.logger.debug(f"[PharmacyAPI] area={area}")
        r = requests.get(PHARMACY_API_URL, params={"area": area}, timeout=5)
        app.logger.debug(f"[PharmacyAPI] {r.status_code} {r.text[:200]}")
        return r.json()
    except:
        return {"error": "Σφάλμα σύνδεσης με API φαρμακείων."}

def get_hospital_info() -> str:
    try:
        r = requests.post(HOSPITAL_API_URL, json={}, timeout=5)
        return r.json().get("fulfillmentText", "Δεν βρέθηκαν πληροφορίες.")
    except:
        return "Σφάλμα API νοσοκομείων."

def get_distance_fare(orig="Πάτρα", dest="Αθήνα") -> dict:
    try:
        r = requests.post(DISTANCE_API_URL,
                          json={"origin": orig, "destination": dest},
                          timeout=8)
        return r.json()
    except:
        return {"error": "Σφάλμα API διαδρομής."}

# --- Formatters & Flows ---
def format_pharmacies(data: dict) -> str:
    if "error" in data:
        return data["error"]
    lst = data.get("pharmacies", [])
    if not lst:
        return "Δεν βρέθηκαν εφημερεύοντα φαρμακεία."
    return "Εφημερεύοντα φαρμακεία:\n" + "\n".join(
        f"- {p['name']}, {p['address']} ({p['time_range']})" for p in lst
    )

def handle_distance_flow(msg: str, sid: str) -> str:
    dest = extract_destination(msg)
    if not dest:
        SESSIONS[sid] = {"pending": "TripCostIntent"}
        return "Σε ποιον προορισμό να υπολογίσω απόσταση και κόστος;"
    res = get_distance_fare("Πάτρα", dest)
    if "error" in res:
        return res["error"]
    km, dur = res.get("distance_km"), res.get("duration")
    fare, zone = res.get("total_fare"), res.get("zone")
    txt = (f"Απόσταση Πάτρα→{dest}: {km} χλμ, διάρκεια {dur}, "
           f"κόστος ~{fare}€ (Ζώνη: {zone}).")
    if zone == "zone2":
        txt += "\nΣημείωση: δεν περιλαμβάνονται διόδια/ferry."
    return txt

# --- Main Chat Endpoint ---
@app.route("/chat", methods=["POST"])
def chat():
    payload      = request.get_json()
    user_message = payload.get("message","")
    session_id   = payload.get("session_id","default_session")
    app.logger.debug(f"[User] {session_id}: {user_message!r}")

    session = SESSIONS.get(session_id, {})

    # 1) Ολοκλήρωση TripCost flow
    if session.get("pending") == "TripCostIntent":
        reply = handle_distance_flow(user_message, session_id)
        session.pop("pending")
        SESSIONS[session_id] = session
        return jsonify({"reply": reply})

    # 2) Ολοκλήρωση Pharmacy-area follow-up
    if session.get("pending_pharmacy_area"):
        area = extract_area(user_message)
        session.pop("pending_pharmacy_area")
        if not area:
            return jsonify({"reply":
                "Δεν αναγνώρισα την περιοχή· δοκίμασε π.χ. Ρίο, Οβρυά/Μεσάτιδα, Βραχνέικα, Παραλία Πατρών."})
        reply = format_pharmacies(get_on_duty_pharmacies(area))
        return jsonify({"reply": reply})

    # 3) Ολοκλήρωση Services-follow-up
    if session.get("pending_services"):
        sel = normalize_text(user_message)
        # Καθορισμός απάντησης βάσει επιλογής
        if "εκδρομ" in sel:
            reply = (
                "Οι εκδρομές μας περιλαμβάνουν οργανωμένα πακέτα για Αρχαία Ολυμπία, "
                "Δελφούς, Ναύπακτο, Γαλάξιδι κ.ά., με έμπειρους οδηγούς και "
                "ευέλικτο πρόγραμμα."
            )
        elif "τουριστ" in sel:
            reply = (
                "Οι τουριστικές μεταφορές καλύπτουν υπηρεσίες πόρτα-πόρτα, "
                "ξεναγήσεις και VIP πακέτα σε όλη την Ήπειρο."
            )
        elif "εταιρ" in sel:
            reply = (
                "Οι εταιρικές μεταφορές προσφέρουν "
                "συμβόλαια, reporting και υπηρεσίες μετακίνησης "
                "υπαλλήλων/στελεχών."
            )
        elif "courier" in sel or "κατοικιδ" in sel:
            reply = (
                "Η υπηρεσία Courier & μεταφορά κατοικιδίων "
                "διασφαλίζει ασφαλή παράδοση δεμάτων και "
                "φιλική μετακίνηση κατοικιδίων."
            )
        elif "night" in sel:
            reply = (
                "Το Night Taxi λειτουργεί 00:00–06:00, "
                "με ειδικές βραδινές τιμές και επιπλέον ασφάλεια."
            )
        elif "σχολ" in sel:
            reply = (
                "Τα σχολικά δρομολόγια καλύπτουν παραλαβή/παράδοση "
                "μαθητών με άδεια λειτουργίας & ασφαλιστική κάλυψη."
            )
        else:
            reply = (
                "Δεν κατάλαβα την επιλογή. Διάλεξε μία από τις εξής κατηγορίες:\n"
                "- Εκδρομές\n- Τουριστικές μεταφορές\n- Εταιρικές μεταφορές\n"
                "- Courier & κατοικίδια\n- Night Taxi\n- Σχολικά"
            )
        # Καθαρισμός flag
        session.pop("pending_services")
        SESSIONS[session_id] = session
        return jsonify({"reply": reply})

    # 4) Καταχώρηση νέου intent
    intent = detect_intent(user_message)
    app.logger.debug(f"[Intent] {session_id}: {intent}")

    # 5) Routing
    if intent == "OnDutyPharmacyIntent":
        area = extract_area(user_message)
        if not area:
            SESSIONS[session_id] = {"pending_pharmacy_area": True}
            return jsonify({"reply":
                "Σε ποια περιοχή της Πάτρας ή γύρω απ’ αυτήν θες εφημερεύοντα φαρμακεία; "
                "(π.χ. Ρίο, Οβρυά/Μεσάτιδα, Βραχνέικα, Παραλία Πατρών)"})
        reply = format_pharmacies(get_on_duty_pharmacies(area))

    elif intent == "HospitalIntent":
        reply = get_hospital_info()

    elif intent == "TripCostIntent":
        reply = handle_distance_flow(user_message, session_id)

    elif intent == "PricingInfoIntent":
        nt = normalize_text(user_message)
        if "αποσκευ" in nt or "βαλιτσ" in nt:
            reply = "🔔 Χρέωση αποσκευών >10 kg: 0.39 €/τεμάχιο."
        else:
            reply = (
                "🔔 ΤΙΜΟΛΟΓΙΟ ΤΑΞΙ ΠΑΤΡΑΣ\n"
                "- Ελάχιστη: 4.00€\n"
                "- 0.90€/χλμ εντός πόλης, 1.25€/χλμ εκτός/βράδυ\n"
                "- Ραδιοταξί: 1.92€–5.65€\n"
                "- Αναμονή: 15€/ώρα, +4€ αεροδρόμιο, +1.07€ σταθμός"
            )

    elif intent == "ContactInfoIntent":
        reply = (
            "📞 Taxi Express Πάτρας\n"
            "- Τηλ.: 2610 450000\n"
            "- Booking: https://booking.infoxoros.com"
        )

    elif intent == "ServicesAndToursIntent":
        # Flag για follow-up
        SESSIONS[session_id] = {"pending_services": True}
        reply = (
            "Με ποιες από τις παρακάτω υπηρεσίες σε ενδιαφέρει να σε βοηθήσω;\n"
            "- Εκδρομές\n- Τουριστικές μεταφορές\n- Εταιρικές μεταφορές\n"
            "- Courier & μεταφορά κατοικιδίων\n- Night Taxi\n- Σχολικά"
        )

    elif intent == "EndConversationIntent":
        reply = "Ευχαριστούμε πολύ για την επικοινωνία! Καλή συνέχεια και ασφαλείς διαδρομές."

    else:
        # GPT Fallback
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role":"system", "content": SYSTEM_PROMPT},
                {"role":"user",   "content": user_message}
            ]
        )
        reply = resp.choices[0].message.content.strip()

    return jsonify({"reply": reply})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
