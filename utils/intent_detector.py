# services/intent_detector.py

import re
import unicodedata


def strip_accents(text):
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def detect_intent_and_entities(message):
    msg = strip_accents(message.lower())

    # 🔹 Distance intent detection
    cost_keywords = [
        "πόσο κοστίζει",
        "πόσο στοιχίζει",
        "τιμή ταξί",
        "πόσο κάνει",
        "πόσο πάει",
        "τι χρεώνει",
        "τιμή",
        "πόσα χρήματα",
        "πόσο πληρώνω",
        "πόσα λεφτά",
        "κόστος",
        "πόσα χιλιόμετρα",
        "πόση απόσταση",
    ]
    if any(kw in msg for kw in cost_keywords):
        return "distance_fare", [message]  # pass full message to API

    # 🔹 Pharmacy
    if any(
        word in msg
        for word in ["φαρμακ", "εφημερ", "διανυκτ", "pharmacy", "pharmakeio"]
    ):
        return "pharmacy", []

    # 🔹 Hospital
    if any(word in msg for word in ["νοσοκομ", "hospital", "παίδων", "ανδρεας"]):
        return "hospital", []

    # 🔹 Minimum fare
    if any(
        word in msg
        for word in [
            "ελάχιστη",
            "σημαία",
            "start",
            "flag",
            "πτωσ",
            "πτώση",
            "minimum",
            "βασική",
            "πρώτη χρέωση",
        ]
    ):
        return "minimum_fare", []

    # 🔹 Tariff info
    if any(
        word in msg
        for word in ["ταρίφα", "τιμολόγιο", "νυχτερινή", "διπλή ταρίφα", "κοστολόγιο"]
    ):
        return "fare_info", []

    # 🔹 Wait fare
    if "αναμον" in msg:
        return "wait_fare", []

    return "default", []
