import random

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

# Μπορείς να προσθέσεις και άλλα, π.χ. contact/pharmacy responses

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
