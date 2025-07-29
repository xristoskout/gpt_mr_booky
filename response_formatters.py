# response_formatters.py

from typing import Any, Dict

def format_pharmacies(data: Dict[str, Any]) -> str:
    """
    Turn the raw JSON from the Pharmacy API into a user-friendly string.
    """
    if err := data.get("error"):
        return err

    pharmacies = data.get("pharmacies", [])
    if not pharmacies:
        return "Δεν βρέθηκαν εφημερεύοντα φαρμακεία."

    lines = ["Εφημερεύοντα φαρμακεία:"]
    for p in pharmacies:
        name    = p.get("name", "").strip()
        address = p.get("address", "").strip()
        time    = p.get("time_range", "").strip()
        lines.append(f"- {name}, {address} ({time})")
    return "\n".join(lines)
