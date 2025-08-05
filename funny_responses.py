import random

# Υπάρχουσες χιουμοριστικές απαντήσεις για κόστη διαδρομών
def trip_cost_response(price: float, destination: str) -> str:
    templates = [
        f"Για {destination}; Μας κοστίζει μόλις {price:.2f}€ — πιο φτηνά από πίτσα 🍕.",
        f"Η ταρίφα για {destination} είναι {price:.2f}€. Δεν λέω, ακούγεται τίμιο! 😉",
        f"{price:.2f}€ μέχρι {destination}. Και χωρίς καμένα λάδια στο αυτοκίνητο, υποσχόμαστε 😅.",
        f"Μέχρι {destination}; Θα σου κοστίσει {price:.2f}€. Το GPS υπόσχεται να μην κάνει κύκλους."
    ]
    return random.choice(templates)

def funny_trip_response(from_city, to_city, price):
    templates = [
        f"Από {from_city} προς {to_city}; Μόνο {price}€! Και ναι, περιλαμβάνει και... air condition. 😉",
        f"{price}€ για να πας {to_city}; Φτηνότερο από ψώνια στο σούπερ μάρκετ!",
        f"Με {price}€ πας {to_city} και δεν χρειάζεται να οδηγήσεις εσύ. Τι άλλο θες;"
    ]
    return random.choice(templates)

# Υπάρχουσα χιουμοριστική απόκριση στοιχείων επικοινωνίας
def funny_contact_response() -> str:
    return random.choice([
        "📞 Taxi Express Πάτρας: 2610 450 000 · Booking: https://booking.infoxoros.com\n(Και γίναμε πύραυλος 🕊️)",
        "Θες να κλείσεις ταξί; 📱 Κάλεσέ μας στο 2610 450 000 ή μπες στο https://booking.infoxoros.com. Ταξί με στυλ!",
        "Μπορείς να μας βρεις στο 2610 450 000 — μην το ψάχνεις, είμαστε πάντα εκεί 😉"
    ])

def pricing_info_response(min_fare: float, bag_rate: float, wait_rate: float) -> str:
    return (
        f"🔔 Ελάχιστη χρέωση: {min_fare:.2f}€\n"
        f"- Αποσκευή >10kg: {bag_rate:.2f}€/τεμάχιο\n"
        f"- Αναμονή: {wait_rate:.2f}€/ώρα\n"
        f"(ή αν θες να το πούμε πιο ποιητικά: «τα ταξί κοστίζουν, όπως και ο χρόνος!»)"
    )

# ── ΝΕΕΣ ΧΙΟΥΜΟΡΙΣΤΙΚΕΣ ΣΥΝΑΡΤΗΣΕΙΣ ──

def funny_pharmacy_response(pharmacies):
    """
    Παράγει χιουμοριστική απάντηση για εφημερεύοντα φαρμακεία.
    Αν η λίστα είναι άδεια, κάνει ένα αστείο σχόλιο.
    """
    if not pharmacies:
        return "😴 Δεν βρήκαμε ανοιχτά φαρμακεία, μάλλον όλοι κοιμούνται!"
    lines = [f"💊 {p['name']} ({p['address']}) {p['time_range']}" for p in pharmacies]
    return (
        "🩺 Εφημερεύουν τα εξής φαρμακεία:\n" +
        "\n".join(lines) +
        "\n🤗 Περαστικά και καλή ανάρρωση!"
    )

def funny_hospital_response(hospitals, on_call_message: str) -> str:
    """
    Παράγει χιουμοριστική απάντηση για νοσοκομεία.
    Προσθέτει προτροπή για ζώνη ασφαλείας.
    """
    if not hospitals:
        return "🚑 Δεν εντοπίσαμε νοσοκομεία σε εφημερία αυτή τη στιγμή. Ελπίζω να μη το χρειαστείς!"
    resp = "🏥 Σήμερα εφημερεύουν:\n"
    for h in hospitals:
        resp += f"- {h['name']} ({h['address']}, τηλ: {h['phone']})\n"
    resp += f"🕐 {on_call_message}\n💉 Μην ξεχνάς να φοράς ζώνη ασφαλείας!"
    return resp

def funny_services_response(summary: str, services: dict, tours: list) -> str:
    """
    Παράγει χιουμοριστική απάντηση για τις υπηρεσίες/εκδρομές.
    """
    reply = summary + "\n"
    for val in services.values():
        reply += f"{val}\n"
    for tour in tours:
        reply += (
            f"{tour['title']} – {tour['price']} – {tour['duration']}\n"
            f"Περιλαμβάνει: {tour['includes']}\n"
            f"Δεν περιλαμβάνει: {tour['not_included']}\n"
        )
    reply += "🧳 Έτοιμος για μια αξέχαστη περιπέτεια;"
    return reply.strip()

def funny_patras_response(answer) -> str:
    """
    Προσθέτει αστείο επίλογο σε απαντήσεις του Patras LLM Answers.
    Δέχεται είτε string είτε dict (με κλειδί 'answer' ή 'reply').
    """
    if isinstance(answer, dict):
        answer = answer.get("answer") or answer.get("reply") or str(answer)
    return answer.strip() + "\n😉 Ελπίζω να σε βοήθησα!"

def funny_default_response() -> str:
    """
    Γενική fallback απάντηση με χιούμορ.
    """
    return random.choice([
        "😅 Χμμ… δεν το έπιασα. Μήπως να το ξαναπείς;",
        "🧐 Δεν κατάλαβα, αλλά είμαι εδώ για να βοηθήσω!",
        "🤖 Κάτι δεν μου κολλάει. Για προσπάθησε με άλλα λόγια."
    ])
