import os
import re
import json
import logging
import requests
import spacy
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Request, Response
from pydantic import BaseModel
from dotenv import load_dotenv

from config import Settings
from api_clients import build_clients
from intents import IntentClassifier
from constants import SYSTEM_PROMPT

# === Init Logger ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Load NLP ===
# Προσπαθούμε να φορτώσουμε το ελληνικό μοντέλο, αλλιώς προεπιλογή στα αγγλικά
try:
    spacy.load("el_core_news_sm")
except Exception:
    spacy.load("en_core_web_sm")

# === Load ENV / Config ===
load_dotenv()
settings = Settings()

# === Init FastAPI & Settings ===
app = FastAPI()

# === Handle CORS ===
origins = ["*"] if settings.cors_origins == "*" else [o.strip() for o in settings.cors_origins.split(",")]
logger.info(f"CORS Origins Loaded: {origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=(origins != ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

# === Init Clients and Classifier ===
clients = build_clients(settings)
classifier = IntentClassifier(settings.intents_path)

# === Load local JSON knowledge at startup ===
@app.on_event("startup")
def load_local_knowledge():
    app.state.knowledge_base = {}
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

# === Models ===
class ChatRequest(BaseModel):
    message: str

# === Helper functions ===
def ask_llm_with_system_prompt(user_message: str, context_text: str) -> str:
    """
    Καλεί το OpenAI API χρησιμοποιώντας το SYSTEM_PROMPT και επιστρέφει τη χιουμοριστική απάντηση.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{user_message}\n\nΠληροφορίες για βοήθεια:\n{context_text}",
        },
    ]
    payload = {
        "model": "gpt-3.5-turbo",
        "messages": messages,
        "temperature": 0.7,
        "presence_penalty": 0.6,
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
        return "⚠ Σφάλμα στην επικοινωνία με το LLM."

def simple_location_extractor(text: str):
    """
    Ενισχυμένο extraction για ελληνικά!
    - Πρώτα βρίσκει full routes: "απο Πάτρα μέχρι Αθήνα"
    - Μετά βρίσκει απλά "στην/στο/για/προς Αθήνα"
    - Αν βρει μόνο μια λέξη (π.χ. "Αθήνα") τη θεωρεί προορισμό, με default FROM = Πάτρα
    """
    pattern = r"απο\s+(?P<from>\w+).*?(?:μεχρι|μέχρι|προς|για|εως)\s+(?P<to>\w+)"
    match = re.search(pattern, text.lower())
    if match:
        return {"FROM": match.group("from").capitalize(), "TO": match.group("to").capitalize()}
    loc_match = re.search(r"(?:στην|στη|στο|για|προς)\s+(?P<to>\w+)", text.lower())
    if loc_match:
        return {"FROM": "Πάτρα", "TO": loc_match.group("to").capitalize()}
    single_word = text.strip().capitalize()
    if single_word and len(single_word) < 20 and all(c.isalpha() for c in single_word):
        return {"FROM": "Πάτρα", "TO": single_word}
    return {}

# === ROUTES ===
@app.post("/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    user_message = body.get("message", "")
    result = classifier.detect(user_message)
    intent = result.get("intent")
    entities = result.get("entities", {})
    logger.info(f"[INTENT]: {intent}, [ENTITIES]: {entities}")
    kb = app.state.knowledge_base

    # Contact info
    if intent == "ContactInfoIntent":
        contact_base = kb.get("contact", {})
        info = contact_base.get("contact", {})
        if info:
            context = (
                f"{contact_base.get('organization', 'Taxi Express Πάτρας')}\n"
                f"Τηλέφωνο: {info.get('phone')}\n"
                f"Site: {info.get('website')}\n"
                f"Email: {info.get('email')}\n"
                f"Διεύθυνση: {info.get('address')}"
            )
        else:
            context = "Δεν υπάρχουν στοιχεία επικοινωνίας στη βάση δεδομένων."
        reply = ask_llm_with_system_prompt(user_message, context)
        return {"reply": reply}

    # Εκδρομές/υπηρεσίες
    if intent == "ServicesAndToursIntent":
        tours = kb.get("services_and_tours", {})
        if tours:
            text = tours.get("summary", "") + "\n"
            for val in tours.get("services", {}).values():
                text += f"{val}\n"
            for tour in tours.get("tours", []):
                text += (
                    f"{tour['title']} – {tour['price']} – {tour['duration']}\n"
                    f"Περιλαμβάνει: {tour['includes']}\n"
                    f"Δεν περιλαμβάνει: {tour['not_included']}\n"
                )
        else:
            text = "Δεν βρέθηκαν πληροφορίες για εκδρομές."
        reply = ask_llm_with_system_prompt(user_message, text.strip())
        return {"reply": reply}

    # Πατρα LLM Answers (generic info)
    if intent == "PatrasLlmAnswersIntent":
        try:
            resp = clients["patras-llm-answers"].answer(user_message)
            # resp μπορεί να είναι dict ή string
            if isinstance(resp, dict):
                base_answer = resp.get("answer") or resp.get("reply") or json.dumps(resp, ensure_ascii=False)
            else:
                base_answer = resp
            reply = ask_llm_with_system_prompt(user_message, base_answer)
            return {"reply": reply}
        except Exception as e:
            logger.error(f"Patras LLM Answers client error: {e}")
            return {"reply": "❌ Σφάλμα αναζήτησης πληροφοριών."}

    # Εφημερεύοντα φαρμακεία
    if intent == "OnDutyPharmacyIntent":
        area = entities.get("AREA", "Πάτρα")
        try:
            response = clients["pharmacy"].get_on_duty(area=area, method="get")
            pharmacies = response.get("pharmacies", [])
            if pharmacies:
                context = "\n".join([f"{p['name']} – {p['address']} ({p['time_range']})" for p in pharmacies])
            else:
                context = f"Δεν βρέθηκαν εφημερεύοντα φαρμακεία στην περιοχή {area}."
            reply = ask_llm_with_system_prompt(user_message, context)
            return {"reply": reply}
        except Exception as e:
            logger.error(f"Pharmacy client error: {e}")
            return {"reply": "❌ Σφάλμα συστήματος φαρμακείων."}

    # Εφημερεύοντα νοσοκομεία
    if intent == "HospitalIntent":
        try:
            resp = clients["hospital"].info()
            if isinstance(resp, str):
                context = resp
            else:
                # Αναμένουμε δομή {"hospitals": [...], "on_call_message": "..."}
                hospitals = resp.get("hospitals", [])
                on_call_msg = resp.get("on_call_message", "")
                if hospitals:
                    context = "\n".join([f"{h['name']} – {h['address']} ({h.get('phone','')})" for h in hospitals])
                    if on_call_msg:
                        context += f"\n{on_call_msg}"
                else:
                    context = "Δεν υπάρχουν διαθέσιμες πληροφορίες νοσοκομείων."
            reply = ask_llm_with_system_prompt(user_message, context)
            return {"reply": reply}
        except Exception as e:
            logger.error(f"Hospital client error: {e}")
            return {"reply": "❌ Σφάλμα στο σύστημα νοσοκομείων."}

    # Υπολογισμός κόστους ταξιδιού
    if intent == "TripCostIntent":
        origin = entities.get("FROM")
        destination = entities.get("TO")
        # Κάνε έξτρα extraction αν δεν βρέθηκε TO
        if not destination:
            fallback = simple_location_extractor(user_message)
            origin = origin or fallback.get("FROM", "Πάτρα")
            destination = fallback.get("TO")
        # Αν δόθηκε μόνο προορισμός (μία λέξη)
        if not destination and len(user_message.strip().split()) == 1 and user_message.strip().isalpha():
            origin = "Πάτρα"
            destination = user_message.strip().capitalize()
        if destination:
            travel_kb = kb.get("travel_costs", {})
            cost_info = travel_kb.get(destination.lower())
            if cost_info:
                base_text = f"Απόσταση {origin}–{destination}: κόστος {cost_info['cost']}€"
                reply = ask_llm_with_system_prompt(user_message, base_text)
                return {"reply": reply}
            else:
                try:
                    # καταγράφουμε τι στέλνουμε στο timologio για debugging
                    logger.debug(f"Timologio request: origin={origin}, destination={destination}")
                    result = clients["timologio"].calculate({"origin": origin, "destination": destination})
                    # Προσπαθούμε να βρούμε τιμή στο αποτέλεσμα
                    price = None
                    for key in ("total_fare", "total_cost", "fare"):
                        if key in result:
                            price = result[key]
                            break
                    if price is not None:
                        context = f"Κόστος {origin}–{destination}: {price}€"
                    else:
                        # Αν δεν υπάρχει τιμή, χρησιμοποιούμε όλο το JSON
                        context = json.dumps(result, ensure_ascii=False)
                    reply = ask_llm_with_system_prompt(user_message, context)
                    return {"reply": reply}
                except Exception as e:
                    logger.error(f"TripCost client error: {e}")
                    return {"reply": "❌ Σφάλμα υπολογισμού κόστους."}
        # Χρειάζεται προορισμός
        return {"reply": ask_llm_with_system_prompt(user_message, "Δεν δόθηκε προορισμός.")}

    # Αν δεν αναγνωρίστηκε πρόθεση – fallback
    unknown_reply = ask_llm_with_system_prompt(user_message, "Δεν αναγνωρίζω αυτήν την ερώτηση.")
    return {"reply": unknown_reply}

# === Health Endpoints ===
@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/healthz")
def healthz():
    return Response("OK", media_type="text/plain")

def create_app():
    return app
