# session_manager.py

import time

SESSION_TIMEOUT = 600  # 10 λεπτά
MAX_HISTORY = 3

INTENT_SLOTS = {
    "TripCostIntent": ["origin", "destination"],
    "OnDutyPharmacyIntent": ["area"],
    "HospitalIntent": ["day"],
    "ServicesAndToursIntent": ["type"],
    "ContactInfoIntent": ["ways"],
    "PatrasLlmAnswersIntent": ["how"],
}

SLOT_PROMPTS = {
    ("TripCostIntent", "origin"): "Ποια είναι η αφετηρία σας;",
    ("TripCostIntent", "destination"): "Ποιος είναι ο προορισμός σας;",
    ("OnDutyPharmacyIntent", "area"): "Για ποια περιοχή θέλετε το φαρμακείο;",
    ("HospitalIntent", "day"): "Ποια μέρα θέλετε το εφημερεύον νοσοκομείο;",
    ("ServicesAndToursIntent", "type"): "Τι είδους εκδρομή ή υπηρεσία σας ενδιαφέρει;",
    ("ContactInfoIntent", "ways"): "Με ποιόν τρόπο θα θέλατε να επικοινωνήσετε;",
    ("PatrasLlmAnswersIntent", "how"): "Πείτε μου περισσότερα για το πώς να σας βοηθήσω.",
}

class SessionManager:
    def __init__(self):
        self.sessions = {}

    def get_session(self, user_id):
        now = time.time()
        session = self.sessions.get(user_id)
        if session and now - session['last_used'] < SESSION_TIMEOUT:
            session['last_used'] = now
            return session
        new_sess = {
            "active_intent": None,
            "slots": {},
            "history": [],
            "last_used": now,
        }
        self.sessions[user_id] = new_sess
        return new_sess

    def update_slot(self, user_id, intent, slot, value):
        sess = self.get_session(user_id)
        sess['active_intent'] = intent
        if intent not in sess['slots']:
            sess['slots'][intent] = {}
        sess['slots'][intent][slot] = value

    def get_missing_slots(self, user_id, intent):
        sess = self.get_session(user_id)
        filled = sess['slots'].get(intent, {})
        required = INTENT_SLOTS.get(intent, [])
        return [slot for slot in required if slot not in filled or not filled[slot]]

    def set_active_intent(self, user_id, intent):
        sess = self.get_session(user_id)
        sess['active_intent'] = intent

    def get_active_slots(self, user_id, intent):
        sess = self.get_session(user_id)
        return sess['slots'].get(intent, {})

    def clear_slots(self, user_id, intent):
        sess = self.get_session(user_id)
        if intent in sess['slots']:
            del sess['slots'][intent]

    def add_history(self, user_id, intent, user_msg, bot_msg):
        sess = self.get_session(user_id)
        sess['history'].append({
            "intent": intent,
            "user": user_msg,
            "bot": bot_msg
        })
        sess['history'] = sess['history'][-MAX_HISTORY:]

    def get_history(self, user_id):
        sess = self.get_session(user_id)
        return sess['history']

    def reset(self, user_id):
        if user_id in self.sessions:
            del self.sessions[user_id]

    def overwrite_slot(self, user_id, intent, slot, value):
        sess = self.get_session(user_id)
        if intent not in sess['slots']:
            sess['slots'][intent] = {}
        sess['slots'][intent][slot] = value

    def get_last_intent(self, user_id):
        sess = self.get_session(user_id)
        if sess['history']:
            return sess['history'][-1]['intent']
        return sess.get('active_intent', None)

FALLBACK_PROMPTS = [
    "Συγγνώμη, δεν το κατάλαβα. Θέλετε να το ξαναδιατυπώσετε;",
    "Δεν είμαι σίγουρος ότι κατάλαβα, μπορείτε να μου δώσετε περισσότερες πληροφορίες;",
    "Δοκιμάστε να μου πείτε με άλλα λόγια τι χρειάζεστε!",
    "Μπορείτε να μου δώσετε λίγες ακόμη λεπτομέρειες;",
]
