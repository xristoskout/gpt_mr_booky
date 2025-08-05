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

# Εισάγουμε τις νέες συναρτήσεις
from funny_responses import (
    funny_trip_response,
    trip_cost_response,
    funny_contact_response,
    funny_pharmacy_response,
    funny_hospital_response,
    funny_services_response,
    funny_patras_response,
    funny_default_response,
)

# === Init Logger ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Load NLP ===
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
def safe_reply(result):
    return result if isinstance(result, dict) and "reply" in result else {"reply": str(result)}

def simple_location_extractor(text: str):
    """
    Ενισχυμένο extraction για ελληνικά!
    - Πρώτα βρίσκει full routes: "απο Πάτρα μέχρι Αθήνα"
    - Μετά βρίσκει απλά "στην/στο/για/προς Αθήνα"
    - Αν βρει μόνο μια λέξη (π.χ. "Αθήνα") τη θεωρεί προορισμό, με default FROM = Πάτρα
    """
    # pattern με ονομαστικά groups
    pattern = r"απο\s+(?P<from>\w+).*?(?:μεχρι|προς|για|εως|μέχρι)\s+(?P<to>\w+)"
    match = re.search(pattern, text.lower())
    if match:
        return {"FROM": match.group("from").capitalize(), "TO": match.group("to").capitalize()}
    # Fallback: "στην/στο/για/προς ..." -> μόνο TO
    loc_match = re.search(r"(?:στην|στη|στο|για|προς)\s+(?P<to>\w+)", text.lower())
    if loc_match:
        return {"FROM": "Πάτρα", "TO": loc_match.group("to").capitalize()}
    # Single word ως TO (μόνο προορισμός)
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

    # --- Intents processing ---
    if intent == "ContactInfoIntent":
        contact_base = kb.get("contact", {})
        info = contact_base.get("contact", {})
        if info:
            # αν υπάρχει βάση δεδομένων, επιστρέφουμε τα πλήρη στοιχεία
            return {
                "reply": (
                    f"🚖 {contact_base.get('organization', 'Taxi Express Πάτρας')}\n"
                    f"📞 {info.get('phone')}\n"
                    f"🌐 {info.get('website')}\n"
                    f"📧 {info.get('email')}\n"
                    f"📍 {info.get('address')}"
                )
            }
        return {"reply": funny_contact_response()}

    elif intent == "ServicesAndToursIntent":
        tours = kb.get("services_and_tours", {})
        if tours:
            return {"reply": funny_services_response(
                tours.get("summary", ""),
                tours.get("services", {}),
                tours.get("tours", []),
            )}

    elif intent == "PatrasLlmAnswersIntent":
        try:
            resp = clients["patras-llm-answers"].answer(user_message)
            return {"reply": funny_patras_response(resp)}
        except Exception as e:
            logger.error(f"Patras LLM Answers client error: {e}")
            return {"reply": "❌ Σφάλμα αναζήτησης πληροφοριών."}

    elif intent == "OnDutyPharmacyIntent":
        area = entities.get("AREA", "Πάτρα")
        try:
            response = clients["pharmacy"].get_on_duty(area=area, method="get")
            pharmacies = response.get("pharmacies", [])
            return {"reply": funny_pharmacy_response(pharmacies)}
        except Exception as e:
            logger.error(f"Pharmacy client error: {e}")
            return {"reply": "❌ Σφάλμα συστήματος φαρμακείων."}

    elif intent == "HospitalIntent":
        try:
            resp = clients["hospital"].info()
            # αν η απάντηση είναι string, προσθέτουμε απλώς χιουμοριστικό επίλογο
            if isinstance(resp, str):
                return {"reply": resp + "\n💉 Μην ξεχνάς να φοράς ζώνη ασφαλείας!"}
            # αν επιστραφεί δομημένο dict:
            hospitals = resp.get("hospitals", [])
            on_call_msg = resp.get("on_call_message", "")
            return {"reply": funny_hospital_response(hospitals, on_call_msg)}
        except Exception as e:
            logger.error(f"Hospital client error: {e}")
            return {"reply": "❌ Σφάλμα στο σύστημα νοσοκομείων."}

    elif intent == "TripCostIntent":
        origin = entities.get("FROM")
        destination = entities.get("TO")
        # Κάνε έξτρα extraction αν δεν βρέθηκε TO
        if not destination:
            fallback = simple_location_extractor(user_message)
            origin = origin or fallback.get("FROM", "Πάτρα")
            destination = fallback.get("TO")
        # Αν γράψει απλά προορισμό (π.χ. "Αθήνα")
        if not destination and len(user_message.strip().split()) == 1 and user_message.strip().isalpha():
            origin = "Πάτρα"
            destination = user_message.strip().capitalize()
        if destination:
            travel_kb = kb.get("travel_costs", {})
            cost_info = travel_kb.get(destination.lower())
            if cost_info:
                # χρησιμοποιούμε το χιουμοριστικό trip_response
                return {"reply": funny_trip_response(origin, destination, cost_info['cost'])}
            else:
                try:
                    result = clients["timologio"].calculate({"origin": origin, "destination": destination})
                    # αν το API μας επιστρέψει το κόστος, το περνάμε στη χιουμοριστική απόκριση
                    price = None
                    for key in ("total_fare", "total_cost", "fare"):
                        if key in result:
                            price = result[key]
                            break
                    if price is not None:
                        return {"reply": funny_trip_response(origin, destination, price)}
                    return {"reply": funny_patras_response(result)}
                except Exception as e:
                    logger.error(f"TripCost client error: {e}")
                    return {"reply": "❌ Σφάλμα υπολογισμού κόστους."}
        return {"reply": "❓ Δεν κατάλαβα τον προορισμό. Πού θέλεις να πας;"}

    # Αν δεν αναγνωρίστηκε καμία πρόθεση
    return {"reply": funny_default_response()}

# === Optional OpenAI fallback ===
@app.post("/openai")
def chat_with_openai(chat: ChatRequest):
    try:
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        data = {
            "model": "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": chat.message}],
        }
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=data)
        resp.raise_for_status()
        result = resp.json()
        return {"reply": result["choices"][0]["message"]["content"]}
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return {"reply": "⚠ Σφάλμα OpenAI. Προσπάθησε ξανά."}

# === Health Endpoints ===
@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/healthz")
def healthz():
    return Response("OK", media_type="text/plain")

def create_app():
    return app
