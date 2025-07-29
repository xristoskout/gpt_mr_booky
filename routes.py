# routes.py

import logging
import re
import string
from typing import Any, Dict, Tuple
from pathlib import Path
from constants import SYSTEM_PROMPT
from flask import Blueprint, request, jsonify

from config import Settings
from intents import IntentClassifier
from api_clients import build_clients
from nlp_utils import strip_accents, extract_area
from session_store import get_session, save_session, clear_session
from response_formatters import format_pharmacies

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def normalize_text(text: str) -> str:
    """
    - Καθαρίζει τόνους & σημεία στίξης
    - Μετατρέπει σε πεζά
    - Greeklish‐to‐Greek για βασικούς χαρακτήρες
    """
    t = strip_accents(text)
    t = t.translate(str.maketrans("", "", string.punctuation))
    t = t.lower()
    greeklish_map = {
        "th": "θ", "ph": "φ", "kh": "χ", "ch": "χ", "ps": "ψ",
        "a": "α", "b": "β", "g": "γ", "d": "δ", "e": "ε", "z": "ζ",
        "h": "η", "i": "ι", "k": "κ", "l": "λ", "m": "μ", "n": "ν",
        "o": "ο", "p": "π", "r": "ρ", "s": "σ", "t": "τ", "u": "υ",
        "x": "ξ", "y": "υ", "w": "ω"
    }
    for latin, gr in greeklish_map.items():
        t = re.sub(rf"\b{latin}\b", gr, t)
    return t.strip()


def create_chat_blueprint(settings: Settings) -> Blueprint:
    bp = Blueprint("chat", __name__)
    classifier = IntentClassifier(Path(settings.intents_path))
    clients = build_clients(settings)

    # ——— Follow‐up Handlers ———
    def handle_pharmacy_area(msg: str, sess: Dict) -> Tuple[str, Dict]:
        area = extract_area(msg)
        if not area:
            # Μήνυμα πιο φιλικό, με emoji και παράδειγμα
            return (
                "😕 Δεν κατάλαβα την περιοχή. "
                "Μπορείς να μου πεις σε ποια περιοχή θέλεις να βρω εφημερεύοντα φαρμακεία; "
                "π.χ. «Πάτρα» ή «Ρίο»;",
                {"pending": "pharmacy_area"},
            )
        # Βρήκαμε την περιοχή — δίνουμε λίστα + προτείνουμε άλλη περιοχή
        reply = format_pharmacies(clients["pharmacy"].get_on_duty(area))
        reply += "\n😉 Θέλεις να δοκιμάσουμε και σε κάποια άλλη περιοχή;"
        return reply, {"pending": "pharmacy_other"}


    def handle_pharmacy_other(msg: str, sess: Dict) -> Tuple[str, Dict]:
        area = extract_area(msg)
        if area:
            # Επαναλαμβάνουμε τη λίστα για τη νέα περιοχή
            reply = format_pharmacies(clients["pharmacy"].get_on_duty(area))
            reply += "\n😄 Χρειάζεσαι κάτι άλλο ή να ψάξουμε σε άλλη περιοχή;"
            return reply, {"pending": "pharmacy_other"}
        # Όχι έγκυρη περιοχή
        return (
            "😅 Χμμ… δεν εντόπισα την περιοχή. "
            "Σε ποια περιοχή θα ήθελες να ψάξω για εφημερεύοντα φαρμακεία;",
            {"pending": "pharmacy_other"},
        )

    def handle_hospital_detail(msg: str, sess: Dict) -> Tuple[str, Dict]:
        text = normalize_text(msg)
        if "αύριο" in text or "αυριο" in text:
            info = clients["hospital"].info()
            return (info if isinstance(info, str) else info.get("fulfillmentText", "")), {}
        if "ώρα" in text or "ωρα" in text:
            return "Δυστυχώς δεν έχω ακριβή ώρα λήξης.", {}
        return 'Μπορείς να ρωτήσεις «και αύριο;» ή «μέχρι τι ώρα;»', {"pending": "hospital_detail"}

    def handle_trip_extras(msg: str, sess: Dict) -> Tuple[str, Dict]:
        text = normalize_text(msg)
        last = sess.get("last_route", {})
        if not last:
            return "Δεν βρήκα προηγούμενη διαδρομή.", {}
        extras: Dict[str, Any] = {}
        if "επιστροφή" in text or "επιστροφη" in text:
            extras["round_trip"] = True
        m = re.search(r"(\d+)\s*αποσ", text)
        if m:
            extras["heavy_luggage"] = int(m.group(1))
        m2 = re.search(r"(\d+)\s*λεπ", text)
        if m2:
            extras["wait_minutes"] = int(m2.group(1))
        if not extras:
            return (
                "Τι θέλεις; π.χ. «με 2 αποσκευές», «με 5 λεπτά αναμονής» ή «επιστροφή»;"
            ), {"pending": "trip_extras"}
        payload = {**last, **extras}
        tim = clients["timologio"].calculate(payload)
        if "total_fare" in tim:
            disc = "" if last.get("zone") == "zone1" else " Στην τιμή δεν περιλαμβάνονται διόδια."
            return (
                f"Με αυτές τις ρυθμίσεις η διαδρομή κοστίζει "
                f"{tim['total_fare']:.2f}€ και διαρκεί περίπου {last.get('duration')}." + disc
            ), {}
        return tim.get("error", "Σφάλμα υπολογισμού κόστους."), {}

    FOLLOWUP_HANDLERS = {
        "pharmacy_area": handle_pharmacy_area,
        "pharmacy_other": handle_pharmacy_other,
        "hospital_detail": handle_hospital_detail,
        "trip_extras": handle_trip_extras,
    }

    # ——— /chat endpoint ———
    @bp.route("/chat", methods=["POST"])
    def chat() -> Any:
        data = request.get_json(force=True)
        msg = data.get("message", "").strip()
        sid = data.get("session_id", "default").strip()
        session = get_session(sid) or {}

        nm = normalize_text(msg)
        intent = classifier.detect(msg)

        # fallback greek keywords
        if intent == "default":
            if "φαρμακ" in nm:
                intent = "OnDutyPharmacyIntent"
            elif "νόσοκομ" in nm:
                intent = "HospitalIntent"
            elif any(w in nm for w in ("πόσο", "ποσα", "κόστος")):
                intent = "TripCostIntent"
            elif "ελάχιστ" in nm:
                intent = "PricingInfoIntent"

        # ——— Pending follow‐up: pharmacy flow ———
        if session.get("pending") in ("pharmacy_area", "pharmacy_other"):
            # 1) Έξοδος/τερματισμός συνομιλίας με keywords
            exit_keywords = ("τελος","τέλος","αντιο","αντίο","bye","ok","stop","cancel","ευχαριστ")
            if any(kw in nm for kw in exit_keywords):
                clear_session(sid)
                return jsonify({"reply": "Ευχαριστούμε πολύ! Καλή συνέχεια. 😊"})

            # 2) Αν ο χρήστης ζητήσει άλλο intent (π.χ. νοσοκομείο, τιμή), τότε σπάμε το pharmacy flow
            new_intent = classifier.detect(msg)
            # fallback greek keywords
            if new_intent == "default":
                if "νόσοκομ" in nm:      new_intent = "HospitalIntent"
                elif any(w in nm for w in ("πόσο","κόστος","κοστίζει")):
                    new_intent = "TripCostIntent"
            if new_intent not in ("OnDutyPharmacyIntent",):
                # σπάμε το pharmacy intent και αφήνουμε το main intent να τρέξει
                session.clear()
            else:
                # μένουμε στο pharmacy flow
                reply, new_state = FOLLOWUP_HANDLERS[session["pending"]](msg, session)
                session.clear()
                session.update(new_state)
                save_session(sid, session)
                return jsonify({"reply": reply})

        # ——— Main intents ———
        session.clear()

        if intent == "OnDutyPharmacyIntent":
            area = extract_area(nm)
            if area:
                reply = format_pharmacies(clients["pharmacy"].get_on_duty(area))
                session["pending"] = "pharmacy_other"
            else:
                reply = "Σε ποια περιοχή θες εφημερεύοντα φαρμακεία;"
                session["pending"] = "pharmacy_area"

        elif intent == "HospitalIntent":
            info = clients["hospital"].info()
            reply = info if isinstance(info, str) else info.get("fulfillmentText", "")
            session["pending"] = "hospital_detail"

        elif intent == "PatrasInfoIntent":
            reply = clients["patras"].get_info()


        elif intent == "TripCostIntent":
            m = re.search(r"απο\s+(.*?)\s+μεχρι\s+(.+)", nm)
            if m:
                orig, dest = m.group(1), m.group(2)
            else:
                orig, dest = "Πάτρα", nm.split()[-1]
            orig, dest = orig.title(), dest.title()
            route = clients["distance"].route_and_fare(orig, dest)
            if "error" in route:
                reply = "Δεν κατάλαβα για ποια διαδρομή ρωτάς."
            else:
                session["last_route"] = route
                info = clients["timologio"].calculate(route)
                if "total_fare" in info:
                    disc = "" if route["zone"] == "zone1" else " Στην τιμή δεν περιλαμβάνονται διόδια."
                    reply = (
                        f"Η διαδρομή κοστίζει {info['total_fare']:.2f}€ "
                        f"και διαρκεί περίπου {route['duration']}." + disc
                    )
                    session["pending"] = "trip_extras"
                else:
                    reply = info.get("error", "Σφάλμα υπολογισμού κόστους.")

        elif intent == "PricingInfoIntent":
            reply = (
                f"🔔 Ελάχιστη χρέωση: {settings.min_fare:.2f}€\n"
                f"- Αποσκευή >10kg: {settings.baggage_rate:.2f}€/τεμάχιο\n"
                f"- Αναμονή: {settings.wait_rate:.2f}€/ώρα"
            )

        elif intent == "ContactInfoIntent":
            reply = "📞 Taxi Express Πάτρας: 2610 450 000 · Booking: https://booking.infoxoros.com"

        elif intent == "EndConversationIntent":
            clear_session(sid)
            reply = "Ευχαριστούμε πολύ που επικοινωνήσατε! Καλή συνέχεια."

        else:
            # Fallback σε OpenAI με το φιλικό System Prompt
            comp = clients["openai"].chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": msg},
                ],
                temperature=0.7,
                presence_penalty=0.6,
                frequency_penalty=0.2,
            )
            reply = comp.choices[0].message.content.strip()

        save_session(sid, session)
        return jsonify({"reply": reply})

    @bp.route("/healthz", methods=["GET"])
    def healthz():
        return jsonify({"status": "ok"}), 200

    return bp
