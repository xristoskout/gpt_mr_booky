import os
import re
import json
import string
import requests
import unicodedata
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI
from dotenv import load_dotenv
from difflib import SequenceMatcher

# --- Initialization ---
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)
CORS(app)

# --- External API endpoints ---
PHARMACY_API_URL   = "https://pharmacy-api-160866660933.europe-west1.run.app/pharmacy"
DISTANCE_API_URL   = "https://distance-api-160866660933.europe-west1.run.app/calculate_route_and_fare"
HOSPITAL_API_URL   = "https://patra-hospitals-webhook-160866660933.europe-west1.run.app/"
TIMOLOGIO_API_URL  = "https://timologio-160866660933.europe-west1.run.app/calculate_fare"

# --- System prompt (unchanged) ---
SYSTEM_PROMPT = """
Γεια σας! Είμαι ο Mr Booky, ο ψηφιακός βοηθός του Taxi Express Πάτρας (https://taxipatras.com).
- Άμεση εξυπηρέτηση 24/7 – 365 ημέρες το χρόνο
- Στόλος 160 οχημάτων, έμπειροι οδηγοί
- Τηλέφωνο: 2610 450000, Booking: https://booking.infoxoros.com
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
"""

# --- In‐memory session store ---
SESSIONS = {}

# --- Fare constants ---
MIN_FARE       = 4.00   # ελάχιστη χρέωση
CITY_RATE      = 0.90   # €/χλμ εντός πόλης
NIGHT_RATE     = 1.25   # €/χλμ νυχτερινό
RADIO_CALL_FEE = 1.92   # €/ραδιοταξί απλή κλήση
BAGGAGE_RATE   = 0.39   # €/τεμάχιο αποσκευής >10kg

# --- Text normalization helpers ---
def strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", text)
                   if unicodedata.category(ch) != "Mn")

def normalize_text(text: str) -> str:
    t = strip_accents(text.lower())
    return t.translate(str.maketrans("", "", string.punctuation))

# --- Article stripping regex & function ---
ARTICLE_RE = re.compile(r'^(?:τον|την|τη|το|ο|η)\s+', re.IGNORECASE)

def strip_article(text: str) -> str:
    return ARTICLE_RE.sub("", text).strip()

# --- Load NLU intents ---
def load_intents():
    with open("intents.json", encoding="utf-8") as f:
        return json.load(f)
INTENTS = load_intents()

# --- Area aliases for pharmacy fuzzy‐matching ---
RAW_AREA_ALIASES = {
    "πατρα": "Πάτρα", "πατρας": "Πάτρα",
    "παραλια": "Παραλία Πατρών", "παρλια": "Παραλία Πατρών", "παραλ": "Παραλία Πατρών",
    "βραχνεικα": "Βραχνέικα", "βραχνει": "Βραχνέικα", "βραχν": "Βραχνέικα",
    "ριο": "Ρίο", "ριον": "Ρίο",
    "οβρυα": "Μεσσάτιδα", "οβρια": "Μεσσάτιδα", "οβρ": "Μεσσάτιδα",
    "μεσατιδα": "Μεσσάτιδα", "μεσσατιδα": "Μεσσάτιδα", "μεσατ": "Μεσσάτιδα", "μεσσατ": "Μεσσάτιδα",
}
AREA_ALIASES = { normalize_text(k): v for k, v in RAW_AREA_ALIASES.items() }

def extract_area(message: str) -> str | None:
    msg = normalize_text(message)
    for alias, canon in AREA_ALIASES.items():
        if alias in msg or msg in alias:
            return canon
    best_score, best_area = 0.0, None
    for alias, canon in AREA_ALIASES.items():
        score = SequenceMatcher(None, alias, msg).ratio()
        if score > best_score:
            best_score, best_area = score, canon
    return best_area if best_score >= 0.5 else None

# --- Intent classification ---
def fuzzy_intent(msg: str) -> str:
    nm = normalize_text(msg)
    best_score, best_intent = 0.0, "default"
    for intent, data in INTENTS.items():
        for ex in data.get("examples", []):
            score = SequenceMatcher(None, normalize_text(ex), nm).ratio()
            if score > best_score:
                best_score, best_intent = score, intent
    return best_intent if best_score > 0.8 else "default"

def keyword_boosted_intent(msg: str) -> str | None:
    nm = normalize_text(msg)
    if "φαρμακει" in nm:
        return "OnDutyPharmacyIntent"
    if "νοσοκομει" in nm:
        return "HospitalIntent"
    if "αποσκευ" in nm or "βαλιτσ" in nm:
        return "PricingInfoIntent"
    if ("απο" in nm and "μεχρι" in nm) or "κοστιζει" in nm or "χιλιο" in nm or "επιστροφη" in nm:
        return "TripCostIntent"
    if any(k in nm for k in ("τηλεφων", "τηλ", "site", "booking")):
        return "ContactInfoIntent"
    if any(b in nm for b in ("ευχαριστ", "αντιο", "bye")):
        return "EndConversationIntent"
    return None

def detect_intent(message: str) -> str:
    if boosted := keyword_boosted_intent(message):
        return boosted
    if fi := fuzzy_intent(message) != "default":
        return fuzzy_intent(message)
    return "default"

# --- API wrappers ---
def get_on_duty_pharmacies(area: str) -> dict:
    try:
        r = requests.get(PHARMACY_API_URL, params={"area": area}, timeout=5)
        return r.json()
    except:
        return {"error": "Σφάλμα σύνδεσης με το API φαρμακείων."}

def get_hospital_info() -> str:
    try:
        r = requests.post(HOSPITAL_API_URL, json={}, timeout=5)
        return r.json().get("fulfillmentText", "Δεν βρέθηκαν πληροφορίες.")
    except:
        return "Σφάλμα σύνδεσης με το API νοσοκομείων."

def get_distance_fare(origin: str, destination: str) -> dict:
    try:
        r = requests.post(
            DISTANCE_API_URL,
            json={"origin": origin, "destination": destination},
            timeout=8
        )
        return r.json()
    except:
        return {"error": "Σφάλμα σύνδεσης με το API διαδρομών."}

# --- Formatters & Flows ---
def format_pharmacies(data: dict) -> str:
    if data.get("error"):
        return data["error"]
    pharmacies = data.get("pharmacies", [])
    if not pharmacies:
        return "Δεν βρέθηκαν εφημερεύοντα φαρμακεία."
    return "Εφημερεύοντα φαρμακεία:\n" + "\n".join(
        f"- {p['name']}, {p['address']} ({p['time_range']})" for p in pharmacies
    )

def handle_trip(msg: str, session_id: str) -> str:
    nm = normalize_text(msg)
    is_return = "επιστροφη" in nm

    # Extract origin & destination
    m = re.search(r"απο\s+(.*?)\s+μεχρι\s+(.+)", nm)
    if m:
        orig_raw = strip_article(m.group(1).title().strip())
        dest_candidate = m.group(2).strip()
        dest_candidate = re.split(
            r'\b(?:ποσο|χιλιο|κόστος|€|κανει|ευρω)\b',
            dest_candidate, flags=re.IGNORECASE
        )[0].strip()
        dest_raw = strip_article(dest_candidate.title()).rstrip(" ?!.,:")
    else:
        orig_raw, dest_raw = "Πάτρα", None
        for w in reversed(nm.split()):
            if w not in {"απο", "μεχρι", "ως", "για", "εως"}:
                dest_raw = strip_article(w.title())
                break

    if not dest_raw:
        SESSIONS[session_id] = {"pending": "TripCostIntent"}
        return "Σε ποιον προορισμό να υπολογίσω απόσταση ή κόστος;"

    res = get_distance_fare(orig_raw, dest_raw)
    if res.get("error"):
        return f"Δεν βρέθηκε διαδρομή από {orig_raw} προς {dest_raw}. Δοκίμασε ξανά το όνομα."

    if "χιλιο" in nm:
        km = res.get("distance_km")
        return f"Η απόσταση από {orig_raw} προς {dest_raw} είναι {km} χιλιόμετρα."

    total = float(res.get("total_fare", 0.0))
    if is_return:
        total *= 2

    bm = re.search(r"(\d+)\s+αποσκευ", nm)
    if bm:
        total += int(bm.group(1)) * BAGGAGE_RATE

    total = max(total, MIN_FARE)
    return f"Το κόστος διαδρομής από {orig_raw} προς {dest_raw} είναι {total:.2f}€ (χωρίς διόδια)."

# --- /chat endpoint ---
@app.route("/chat", methods=["POST"])
def chat():
    data       = request.get_json()
    message    = data.get("message", "")
    session_id = data.get("session_id", "default")
    session    = SESSIONS.setdefault(session_id, {})

    if session.pop("pending_pharmacy_area", False):
        area = extract_area(message)
        if not area:
            return jsonify({"reply":
                "Δεν αναγνώρισα την περιοχή. Δοκίμασε π.χ. Ρίο, Παραλία Πατρών, Βραχνέικα, Μεσσάτιδα."
            })
        return jsonify({"reply": format_pharmacies(get_on_duty_pharmacies(area))})

    if session.pop("pending", None) == "TripCostIntent":
        return jsonify({"reply": handle_trip(message, session_id)})

    intent = detect_intent(message)
    if intent == "OnDutyPharmacyIntent":
        area = extract_area(message)
        if not area:
            session["pending_pharmacy_area"] = True
            return jsonify({"reply":
                "Σε ποια περιοχή θες να βρω εφημερεύοντα φαρμακεία;"
            })
        reply = format_pharmacies(get_on_duty_pharmacies(area))

    elif intent == "HospitalIntent":
        reply = get_hospital_info()

    elif intent == "PricingInfoIntent":
        reply = (
            f"🔔 Ελάχιστη χρέωση: {MIN_FARE:.2f}€\n"
            f"- Αποσκευές >10kg: {BAGGAGE_RATE:.2f}€/τεμάχιο"
        )

    elif intent == "TripCostIntent":
        reply = handle_trip(message, session_id)

    elif intent == "ContactInfoIntent":
        reply = "📞 Taxi Express Πάτρας: 2610 450000 · https://booking.infoxoros.com"

    elif intent == "EndConversationIntent":
        session.clear()
        reply = "Ευχαριστούμε πολύ που επικοινωνήσατε μαζί μας! Καλή συνέχεια."

    else:
        resp = requests.post(
            TIMOLOGIO_API_URL,
            json={"message": message},
            timeout=5
        )
        if resp.ok:
            data = resp.json()
            if "fare" in data:
                reply = f"Το υπολογιζόμενο κόμιστρο είναι {data['fare']:.2f}€."
            else:
                reply = resp.text
        else:
            r = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": message}
                ]
            )
            reply = r.choices[0].message.content.strip()

    return jsonify({"reply": reply})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
