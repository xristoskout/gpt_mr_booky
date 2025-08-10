# tests/test_flows.py
from main import _clear_state

def post(client, msg, sid):
    payload = {"message": msg, "user_id": sid, "session_id": sid}
    return client.post("/chat", json=payload).json()

def test_pharmacy_sticky_paralia(client, clear_state):
    sid = "t_paralia"; clear_state(sid)
    r1 = post(client, "ποιο φαρμακειο εφημερευει ;", sid)
    assert "περιοχ" in r1["reply"].lower()  # ρωτάει περιοχή
    r2 = post(client, "παραλια", sid)
    txt = r2["reply"].lower()
    assert "περιοχή: παραλία πατρών" in txt
    assert "φαρμακείο" in txt  # ή έστω να έχει λίστα

def test_time_range_not_trip(client, clear_state):
    sid = "t_time"; clear_state(sid)
    r = post(client, "02:00 - 08:30 ΑΓΓΕΛΟΠΟΥΛΟΥ ΕΦΗ — ΑΓ. ΙΩΑΝΝΗ ΠΡΑΤΣΙΚΑ 58", sid)
    txt = r["reply"].lower()
    assert "💶" not in txt  # να μην έβγαλε εκτίμηση ταξί
    # είτε ρώτησε περιοχή είτε έβγαλε λίστα φαρμακείων
    assert ("περιοχ" in txt) or ("φαρμακ" in txt)

def test_area_only_heuristic(client, clear_state):
    sid = "t_area"; clear_state(sid)
    r = post(client, "παραλια", sid)  # μονολεκτικό
    txt = r["reply"].lower()
    # είτε έδωσε κατευθείαν Παραλία Πατρών, είτε τουλάχιστον έμεινε σε PHARMACY flow
    assert ("περιοχή: παραλία πατρών" in txt) or ("φαρμακ" in txt)

def test_zarouhleika_parent_fallback(client, clear_state):
    sid = "t_zar"; clear_state(sid)
    r = post(client, "ζαρουχλεϊκα", sid)
    txt = r["reply"].lower()
    # με το FakePharmacyClient δίνουμε [] → περιμένουμε μήνυμα "δεν βρέθηκαν"
    assert "δεν βρέθηκαν" in txt

def test_trip_quote_patras_athens(client, clear_state):
    sid = "t_trip"; clear_state(sid)
    r = post(client, "πόσο κοστίζει από Πάτρα μέχρι Αθήνα;", sid)
    txt = r["reply"]
    # βασικά στοιχεία
    assert "💶" in txt and "🛣️" in txt and "⏱️" in txt
    # map_url πρέπει να υπάρχει ως πεδίο JSON (το UI σου το κάνει κουμπί)
    assert "map_url" in r and r["map_url"].startswith("https://www.google.com/maps/dir/")

def test_contact_shortcut(client, clear_state):
    sid = "t_contact"; clear_state(sid)
    r = post(client, "τηλέφωνο ραδιοταξί", sid)
    assert "2610" in r["reply"]  # από BRAND_INFO

def test_cancel_resets_state(client, clear_state):
    sid = "t_cancel"; clear_state(sid)
    post(client, "ποιο φαρμακειο εφημερευει ;", sid)
    r = post(client, "άκυρο", sid)
    assert "το αφήνουμε εδώ" in r["reply"].lower()
