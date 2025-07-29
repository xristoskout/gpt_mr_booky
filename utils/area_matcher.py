import unicodedata

AREA_ALIASES = {
    "Πάτρα": ["πάτρα", "patra", "pátra", "πτρα"],
    "Μεσσάτιδα": ["μεσσάτιδα", "μεσατιδα", "messatida", "μεσσατιδα"],
    "Βραχνέικα": ["βραχνέικα", "βραχνει", "vrahneika", "βραχνεϊκα"],
    "Οβρυά": ["οβρυά", "ovria", "οβρια", "οβρυα"],
    "Ρίο": ["ριο", "rio", "ριον", "ριου"],
    "Αθήνα": ["αθήνα", "athina", "athens"],
    "Νοσοκομείο Ρίο": [
        "νοσοκομειο ριον",
        "πανεπιστημιακο νοσοκομειο πατρας",
        "rio hospital",
    ],
    "Καραμανδάνειο": ["καραμανδάνειο", "karamandaneio", "παιδων", "νοσοκομειο παιδων"],
}


def strip_accents(text):
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def fuzzy_match_area(message):
    msg = strip_accents(message.lower())
    for area, aliases in AREA_ALIASES.items():
        for alias in aliases:
            if strip_accents(alias) in msg:
                return area
    return None
