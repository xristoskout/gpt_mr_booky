import os
from flask import Flask, request, jsonify
from openai import OpenAI
from dotenv import load_dotenv
import requests
import unicodedata
import re

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=api_key)

app = Flask(__name__)

PHARMACY_API_URL = "https://pharmacy-api-160866660933.europe-west1.run.app/pharmacy"
DISTANCE_API_URL = "https://distance-api-160866660933.europe-west1.run.app/calculate_route_and_fare"
HOSPITAL_API_URL = "https://patra-hospitals-webhook-160866660933.europe-west1.run.app/"
TIMOLOGIO_API_URL = "https://timologio-160866660933.europe-west1.run.app/calculate_fare"

# --- SYSTEM PROMPT με όλες τις πληροφορίες και FAQ ---
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

# --- Fuzzy location/area aliases ---
AREA_ALIASES = {
    "Πάτρα": ["πάτρα", "patra", "pátra", "πτρα"],
    "Μεσσάτιδα": ["μεσσάτιδα", "μεσατιδα", "messatida", "μεσσατιδα", "μεσσιτιδα"],
    "Βραχνέικα": ["βραχνέικα", "βραχνει", "vrahneika", "βραχνεϊκα", "βραχνεικ", "βραχνεικα"],
    "Οβρυά": ["οβρυά", "ovria", "οβρια", "οβρυα"],
    "Ρίο": ["ριο", "rio", "ριον", "ριου"],
    "Αθήνα": ["αθήνα", "athina", "athens"],
    "Νοσοκομείο Ρίο": [
        "νοσοκομειο ριον", "νοσοκομειο ριο", "πανεπιστημιακο νοσοκομειο πατρας", "gpph", "pgnp",
        "rio hospital", "ριου νοσοκομειο", "νοσοκομειο ριου", "νοσ ριου", "νοσ. ριου"
    ],
    "Άνω Διάκοπτο": ["ανω διακοπτο", "ανω διακοπτ", "διακοπτο"],
    "Αγιος Ανδρέας": ["αγιος ανδρεας", "andreas"],
    "Καραμανδάνειο": ["καραμανδάνειο", "karamandaneio", "παιδων", "νοσοκομειο παιδων"],
}

# --- Fuzzy utilities ---
def strip_accents(text):
    return ''.join(
        c for c in unicodedata.normalize('NFD', text)
        if unicodedata.category(c) != 'Mn'
    )

def fuzzy_match_area(msg):
    msg = strip_accents(msg.lower())
    for area, aliases in AREA_ALIASES.items():
        for alias in aliases:
            if strip_accents(alias) in msg:
                return area
    return None

# --- INTENT DETECTION (επίπεδο φράσης) ---
def detect_intent_and_entities(message):
    msg = strip_accents(message.lower())
    # Κόστος διαδρομής (ταξί)
    cost_keywords = ["πόσο κοστίζει", "πόσο στοιχίζει", "τιμή ταξί", "πόσο κάνει", "πόσο πάει", "τι χρεώνει", "τιμή", "πόσα χρήματα", "πόσο πληρώνω", "πόσα λεφτά", "κοστος", "ποσο ειναι"]
    place_patterns = re.compile(r"(από|απ|απο|μεχρι|για|ως|προς|σε|στο|στη|στην|στον|απο|έως|μέχρι|ως|σε|για) ([^ ]+)", re.IGNORECASE)
    found_cost = any(k in msg for k in cost_keywords)
    # Βρες δυο τοπωνύμια (fuzzy)
    areas_found = []
    for area, aliases in AREA_ALIASES.items():
        for alias in aliases:
            if strip_accents(alias) in msg and area not in areas_found:
                areas_found.append(area)
    if found_cost and len(areas_found) >= 2:
        return "distance_fare", areas_found  # πχ ["Πάτρα", "Αθήνα"]
    # Αν έχει μία τοποθεσία ή λέξη που ταιριάζει μόνο σε φαρμακείο
    pharmacy_kw = ["φαρμακ", "εφημερ", "διανυκτ", "pharmacy", "pharmakeio"]
    if any(word in msg for word in pharmacy_kw):
        return "pharmacy", areas_found
    # Αν ρωτά για νοσοκομείο
    hospital_kw = ["νοσοκομ", "νοσοκ", "hospital", "παίδων", "ανδρεας"]
    if any(word in msg for word in hospital_kw):
        return "hospital", areas_found
    # Ελάχιστη αποζημίωση / σημαία / ραδιοταξί
    min_kw = ["ελάχιστη", "σημαία", "start", "flag", "πτωσ", "πτώση", "minimum", "κουρσα", "βασική", "πρωτη χρεωση"]
    if any(word in msg for word in min_kw):
        return "minimum_fare", []
    # Τιμοκατάλογος/ταρίφα/νυχτερινή
    fare_kw = ["ταρίφα", "τιμολόγιο", "νυχτερινή", "διπλή ταρίφα", "κοστολόγιο"]
    if any(word in msg for word in fare_kw):
        return "fare_info", []
    # Χρέωση αναμονής
    if "αναμονή" in msg or "αναμονης" in msg:
        return "wait_fare", []
    return "default", []

# --- APIs Callers ---
def get_on_duty_pharmacies(area="Πάτρα"):
    params = {"area": area}
    try:
        resp = requests.get(PHARMACY_API_URL, params=params, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("Pharmacy API error:", str(e))
        return {"error": "Το σύστημα εφημερευόντων φαρμακείων δεν είναι διαθέσιμο αυτή τη στιγμή."}

def get_hospital_info():
    try:
        resp = requests.post(HOSPITAL_API_URL, json={}, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        # API μπορεί να έχει fulfillmentText ή άλλο πεδίο
        return data.get("fulfillmentText") or data.get("text") or "Δεν βρέθηκαν νοσοκομεία."
    except Exception as e:
        print("Hospital API error:", str(e))
        return "Το σύστημα εφημερευόντων νοσοκομείων δεν είναι διαθέσιμο αυτή τη στιγμή."

def get_distance_fare(origin, destination):
    data = {"origin": origin, "destination": destination}
    try:
        resp = requests.post(DISTANCE_API_URL, json=data, timeout=8)
        resp.raise_for_status()
        d = resp.json()
        if d.get("error"):
            return None, d.get("error")
        return d, None
    except Exception as e:
        print("Distance API error:", str(e))
        return None, "Το σύστημα διαδρομών δεν είναι διαθέσιμο αυτή τη στιγμή."

def get_timologio_fare(distance_km, time="day", wait_minutes=0, heavy_luggage=0, from_airport=False, from_station=False, radio_call=False, appointment=False, zone="zone1"):
    data = {
        "distance_km": distance_km,
        "time": time,
        "wait_minutes": wait_minutes,
        "heavy_luggage": heavy_luggage,
        "from_airport": from_airport,
        "from_station": from_station,
        "radio_call": radio_call,
        "appointment": appointment,
        "zone": zone
    }
    try:
        resp = requests.post(TIMOLOGIO_API_URL, json=data, timeout=5)
        resp.raise_for_status()
        d = resp.json()
        return d.get("total_fare")
    except Exception as e:
        print("Timologio API error:", str(e))
        return None

# --- Formatters ---
def format_pharmacy_list(data, area):
    if "error" in data:
        return data["error"]
    if not data.get("pharmacies"):
        return f"Δεν βρέθηκαν εφημερεύοντα φαρμακεία στην {area} για τα κριτήρια που δώσατε."
    lines = [f"Σήμερα εφημερεύουν τα παρακάτω φαρμακεία στην {area}:"]
    for p in data["pharmacies"]:
        lines.append(f"- {p['name']}, {p['address']}, {p['time_range']}")
    return "\n".join(lines)

# --- MAIN CHAT ROUTE ---
@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_message = data.get("message", "")
    reply = ""
    try:
        print("User message:", user_message)
        intent, areas = detect_intent_and_entities(user_message)
        print("Intent:", intent, "Areas:", areas)
        if intent == "distance_fare" and len(areas) >= 2:
            origin = areas[0]
            destination = areas[1]
            dist_data, err = get_distance_fare(origin, destination)
            if err:
                reply = err
            elif dist_data:
                km = dist_data.get("distance_km")
                duration = dist_data.get("duration")
                fare = dist_data.get("total_fare")
                zone = dist_data.get("zone")
                txt = f"Απόσταση {origin} προς {destination}: {km} χλμ, διάρκεια {duration}, τιμή {fare}€ (Ζώνη: {zone})."
                if zone == "zone2":
                    txt += "\nΣημείωση: Δεν περιλαμβάνονται πιθανά διόδια και ναύλοι."
                reply = txt
        elif intent == "pharmacy":
            area = areas[0] if areas else "Πάτρα"
            pharmacy_data = get_on_duty_pharmacies(area)
            reply = format_pharmacy_list(pharmacy_data, area)
        elif intent == "hospital":
            reply = get_hospital_info()
        elif intent == "minimum_fare":
            reply = "Η ελάχιστη αποζημίωση για διαδρομή είναι 4,00€ . Για απλή κλήση ραδιοταξί η ελάχιστη είναι 5,92€."
        elif intent == "fare_info":
            reply = ("Τιμολόγιο ταξί Πάτρας:\n"
                "- Ελάχιστη αποζημίωση: 4,00€\n"
                "- Πτώση σημαίας: περιλαμβάνεται στην ελάχιστη\n"
                "- Εντός ζώνης: 0,90€/χλμ\n"
                "- Εκτός ζώνης ή νυχτερινό: 1,25€/χλμ\n"
                "- Ραδιοταξί: απλή κλήση 1,92€, ραντεβού 3,39-5,65€\n"
                "- Αναμονή: 15€/ώρα\n"
                "- Αποσκευές >10kg: 0,39€/τμχ\n"
                "- Από/προς αεροδρόμιο: +4,00€, σταθμό: +1,07€")
        elif intent == "wait_fare":
            reply = "Η χρέωση αναμονής είναι 15€ ανά ώρα."
        else:
            # Default: Περνάει στο OpenAI για FAQ/γενικές πληροφορίες/εκδρομές
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message}
                ]
            )
            reply = response.choices[0].message.content
    except Exception as e:
        print("Exception occurred:", str(e))
        reply = "Λυπάμαι, υπήρξε τεχνικό πρόβλημα. Προσπαθήστε ξανά αργότερα."
    return jsonify({"reply": reply})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
