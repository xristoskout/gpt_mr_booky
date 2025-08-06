import os
import re
import json
import logging
import requests
import spacy
import random
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from dotenv import load_dotenv
from session_manager import SessionManager, SLOT_PROMPTS, FALLBACK_PROMPTS, INTENT_SLOTS
from config import Settings
from api_clients import build_clients
from intents import IntentClassifier
from intents import extract_entities
from constants import SYSTEM_PROMPT
from urllib.parse import quote_plus

sess_mgr = SessionManager()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    spacy.load("el_core_news_sm")
except Exception:
    spacy.load("en_core_web_sm")

load_dotenv()
settings = Settings()

app = FastAPI()
origins = ["*"] if settings.cors_origins == "*" else [o.strip() for o in settings.cors_origins.split(",")]
logger.info(f"CORS Origins Loaded: {origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=(origins != ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

clients = build_clients(settings)
classifier = IntentClassifier(settings.intents_path)

@app.on_event("startup")
def load_local_knowledge():
    app.state.knowledge_base = {}
    app.state.user_sessions = {}
    data_folder = "data"
    if not os.path.exists(data_folder):
        logger.warning("⚠ Ο φάκελος 'data/' δεν βρέθηκε.")
        return
    for filename in os.listdir(data_folder):
        if filename.endswith(".json"):
            key = filename.replace(".json", "")
            try:
                with open(os.path.join(data_folder, filename), "r", encoding="utf-8") as f:
                    app.state.knowledge_base[key] = json.load(f)
                    logger.info(f"✅ Loaded: {filename}")
            except Exception as e:
                logger.error(f"❌ Failed to load {filename}: {e}")

class ChatRequest(BaseModel):
    message: str

def ask_llm_with_system_prompt(user_message: str, context_text: str, history=None) -> str:
    history_context = ""
    if history:
        last_turns = "\n".join([f"Χρήστης: {h['user']}\nBot: {h['bot']}" for h in history[-2:]])
        history_context = f"\n\nΠροηγούμενη συζήτηση:\n{last_turns}\n"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{user_message}\n{history_context}\nΧρήσιμες πληροφορίες:\n{context_text}",
        },
    ]
    payload = {
        "model": "gpt-3.5-turbo",
        "messages": messages,
        "temperature": 0.7,
        "presence_penalty": 0.7,
        "frequency_penalty": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"OpenAI call failed: {e}")
        return "⚠️ Σφάλμα στην επικοινωνία με το LLM. Αν συνεχιστεί, ενημερώστε μας με χιούμορ!"

def powerful_location_extractor(text: str):
    text = text.strip().lower()
    pattern_full = r"απ[οό]\s+([\w\-\s]+?)(?:\s*(?:,|;|\.)?\s*)?(?:μέχρι|μεχρι|έως|εως|ως|για|προς)\s+([\w\-\s]+)"
    m = re.search(pattern_full, text)
    if m:
        return {
            "FROM": m.group(1).strip().capitalize(),
            "TO": m.group(2).strip().capitalize()
        }
    m2 = re.search(r"(?:για|προς|στο|στην|στη)\s+([\w\-\s]+)", text)
    if m2:
        return {
            "FROM": "Πάτρα",
            "TO": m2.group(1).strip().capitalize()
        }
    m3 = re.search(r"απ[οό]\s+([\w\-\s]+)", text)
    if m3:
        return {
            "FROM": m3.group(1).strip().capitalize()
        }
    single_word = text.strip(";,. \n\t").capitalize()
    if single_word and 2 < len(single_word) < 24 and all(c.isalpha() or c in " άέίύόήώϊΐϋΰ" for c in single_word):
        return {
            "FROM": "Πάτρα",
            "TO": single_word
        }
    return {}

@app.post("/chat")
async def chat_endpoint(request: Request):
    try:
        body = await request.json()
        user_message = body.get("message", "")
        user_id = body.get("user_id", "default")
        logger.info(f"clients: {clients}")
        logger.info(f"knowledge_base: {getattr(app.state, 'knowledge_base', None)}")

        # 1. Πάρε intent/entities
     
        last_intent = sess_mgr.get_last_intent(user_id)
        last_missing = (
            sess_mgr.get_missing_slots(user_id, last_intent) if last_intent else []
        )

        normalized = user_message.strip().lower()
        if normalized in {"ναι"}:
            body["confirmed"] = True
            intent = last_intent
            entities = {}
        elif normalized in {"όχι", "οχι"}:
            body["confirmed"] = False
            intent = last_intent
            entities = {}
        else:
            result = classifier.detect(
                user_message, active_intent=last_intent, missing_slots=last_missing
            )
            intent = result.get("intent")
            entities = result.get("entities", {})

        logger.info(f"[INTENT]: {intent}, [ENTITIES]: {entities}")

        # 2. Slot-filling context patch: αν intent=default, unfinished last_intent, και κοντή απάντηση
        short_reply = len(user_message.strip().split()) <= 2

        if last_intent and last_missing and (intent == "default" or short_reply):
            slot_to_fill = last_missing[0]

            # Κάνε extraction από το user_message για να βρεις πιθανό slot value (π.χ. "πάτρα τυρναβος")
            extracted = extract_entities(user_message)
            logger.info(f"[CONTEXT PATCH] Extracted entities: {extracted}")

            value = extracted.get(slot_to_fill) or user_message
            sess_mgr.update_slot(user_id, last_intent, slot_to_fill, value)

            # Ενημέρωσε ΟΛΑ τα slots που βρίσκεις (π.χ. FROM/TO/area)
            for k, v in extracted.items():
                if k != slot_to_fill:
                    sess_mgr.update_slot(user_id, last_intent, k, v)
            intent = last_intent
            for k, v in extracted.items():
                entities[k] = v

            logger.info(
                f"[CONTEXT PATCH]: Used '{value}' as slot value for {slot_to_fill} from '{user_message}'"
            )

        # 3. Πάρε slots για το (σωστό!) intent
        slots = sess_mgr.get_active_slots(user_id, intent)
        missing = sess_mgr.get_missing_slots(user_id, intent)

        # 4. Αν λείπουν ακόμα slots, δοκίμασε ξανά extraction για να καλύψεις πολλά slots με μία απάντηση
        if missing:
            extracted = extract_entities(user_message)
            logger.info(f"[SLOT-FILLING] Trying extraction for missing slots: {extracted}")

            for slot in missing:
                if slot in extracted and extracted[slot]:
                    sess_mgr.update_slot(user_id, intent, slot, extracted[slot])

            slots = sess_mgr.get_active_slots(user_id, intent)
            missing = sess_mgr.get_missing_slots(user_id, intent)

            if missing:
                # prompt για το επόμενο slot που λείπει
                slot = missing[0]
                prompt = SLOT_PROMPTS.get((intent, slot), f"Μπορείς να μου πεις {slot}; 🙏")
                sess_mgr.add_history(user_id, intent, user_message, prompt)
                return {"reply": prompt, "session": sess_mgr.get_session(user_id)}

        # 7. CONFIRMATION (μόλις συμπληρωθούν όλα τα slots)
        required_slots = INTENT_SLOTS.get(intent, [])
        if required_slots and all(s in slots and slots[s] for s in required_slots):
            if not body.get("confirmed"):
                if intent == "TripCostIntent":
                    confirm_text = f"Άρα θέλετε ταξί από {slots.get('origin','-')} προς {slots.get('destination','-')}, σωστά; 🚖"
                elif intent == "OnDutyPharmacyIntent":
                    confirm_text = f"Να σας δείξω εφημερεύον φαρμακείο στην περιοχή {slots.get('area','-')}; 💊"
                elif intent == "HospitalIntent":
                    confirm_text = f"Να σας πω τα εφημερεύοντα νοσοκομεία; 🏥"
                else:
                    confirm_text = f"Να προχωρήσω με τα στοιχεία που μου δώσατε; 😇"
                sess_mgr.add_history(user_id, intent, user_message, confirm_text)
                return {"reply": confirm_text, "ask_confirm": True, "session": sess_mgr.get_session(user_id)}

        # 8. INTENT LOGIC (π.χ. TripCostIntent)
        if intent == "TripCostIntent":
            # Try get from slots/entities as before
            origin = (
                entities.get("FROM")
                or entities.get("origin")
                or entities.get("αφετηρια")
                or slots.get("origin")
                or slots.get("FROM")
                or "Πάτρα"
            )
            destination = (
                entities.get("TO")
                or entities.get("destination")
                or entities.get("προορισμος")
                or slots.get("destination")
                or slots.get("TO")
            )

            # SLOT FILLING PATCH - 1: Check missing origin
            if not origin or origin.strip() == "":
                # Try extract again from user input
                extracted = extract_entities(user_message, context_slot="FROM")
                origin = extracted.get("FROM") or origin
                if not origin or origin.strip() == "":
                    msg = "Ποια είναι η αφετηρία σας;"
                    sess_mgr.add_history(user_id, intent, user_message, msg)
                    return {"reply": msg, "session": sess_mgr.get_session(user_id)}

            # SLOT FILLING PATCH - 2: Check missing destination
            if not destination or destination.strip() == "":
                extracted = extract_entities(user_message, context_slot="TO")
                destination = extracted.get("TO") or destination
                if not destination or destination.strip() == "":
                    msg = "Πού θέλετε να πάτε;"
                    sess_mgr.add_history(user_id, intent, user_message, msg)
                    return {"reply": msg, "session": sess_mgr.get_session(user_id)}

            # Continue with business logic (API call etc)
            logger.info(f"[TripCostIntent] origin: {origin}, destination: {destination}")

            try:
                api_payload = {
                    "sessionInfo": {
                        "parameters": {
                            "origin": origin,
                            "destination": destination
                        }
                    }
                }
                result = clients["timologio"].calculate(api_payload)
                logger.info(f"[TripCostIntent] Timologio API result: {result}")

                price = None
                for key in ("total_fare", "total_cost", "fare", "price"):
                    if key in result:
                        price = result[key]
                        break

                if price is None:
                    msg = (
                        f"Δεν βρήκα τιμή για τη διαδρομή {origin} → {destination}!\n"
                        f"Debug result: {json.dumps(result, ensure_ascii=False)}"
                    )
                    sess_mgr.add_history(user_id, intent, user_message, msg)
                    return {
                        "reply": msg,
                        "session": sess_mgr.get_session(user_id)
                    }

                map_url = f"https://www.google.com/maps/dir/?api=1&origin={quote_plus(origin)}&destination={quote_plus(destination)}&travelmode=driving"
                reply = (
                    f"Η εκτίμηση κόστους από {origin} προς {destination} είναι περίπου **{price}€**.\n"
                    f"[Δείτε τη διαδρομή στο χάρτη]({map_url})"
                )

                sess_mgr.add_history(user_id, intent, user_message, reply)
                sess_mgr.set_active_intent(user_id, None)
                return {
                    "reply": reply,
                    "map_url": map_url,
                    "session": sess_mgr.get_session(user_id)
                }
            except Exception as e:
                logger.exception("[TripCostIntent] API error")
                msg = (
                    "❌ Παρουσιάστηκε σφάλμα στον υπολογισμό του κόστους ταξί. "
                    "Δοκιμάστε άλλη διαδρομή ή ενημερώστε μας!\n"
                    f"({str(e)})"
                )
                sess_mgr.add_history(user_id, intent, user_message, msg)
                return {
                    "reply": msg,
                    "session": sess_mgr.get_session(user_id)
                }

        # ... (other intent logic εδώ αν θες)

        # 9. Fallback (μόνο αν δεν έχεις τίποτα άλλο να κάνεις)
        fallback_msg = random.choice([
            "Δεν το έπιασα, αλλά το παλεύω! Θέλετε να το ξαναπείτε; 😅",
            f"Λίγο μπερδεμένο αυτό... Θέλετε να συνεχίσω με το '{last_intent}' ή κάτι άλλο;",
            f"Δεν κατάλαβα, αλλά υπόσχομαι να το προσπαθήσω ξανά! 🚕"
        ])
        sess_mgr.add_history(user_id, "fallback", user_message, fallback_msg)
        return {"reply": fallback_msg, "session": sess_mgr.get_session(user_id)}

    except Exception as e:
        logger.exception("Test crash in chat endpoint")
        return {"reply": f"❌ Crash: {e}"}



@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/healthz")
def healthz():
    return Response("OK", media_type="text/plain")

def create_app():
    return app
