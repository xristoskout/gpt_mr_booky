# main.py
import re
import logging
from typing import Dict, Any
import requests
import spacy
spacy.load("en_core_web_sm")

from fastapi import FastAPI, Request
from pydantic import BaseModel
from dotenv import load_dotenv

# === Load ENV / Config ===
load_dotenv()
from config import Settings
from api_clients import build_clients
from intents import IntentClassifier
from entity_parser import extract_entities

# === Init ===
settings = Settings()
clients = build_clients(settings)
classifier = IntentClassifier(settings.intents_path)

# === Init Logger & App ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI()

# === Models ===
class ChatRequest(BaseModel):
    message: str

# === Fallback Entity Extractor ===
def simple_location_extractor(text: str):
    pattern = r"απο\s+(?P<from>\w+).*?(?:μεχρι|προς|για|εως)\s+(?P<to>\w+)"
    match = re.search(pattern, text.lower())
    if match:
        return {
            "FROM": match.group("from").capitalize(),
            "TO": match.group("to").capitalize()
        }
    return {}

# === ROUTES ===
@app.post("/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    user_message = body.get("message")

    result = classifier.detect(user_message)
    intent = result["intent"]
    entities = result["entities"]

    logger.info(f"[INTENT]: {intent}, [ENTITIES]: {entities}")

    if intent == "ContactInfoIntent":
        if "ORGANIZATION" in entities and "ΚΤΕΛ" in entities["ORGANIZATION"]:
            return {"response": "📞 Το τηλέφωνο του ΚΤΕΛ Αχαΐας είναι 2610 623 886"}
        return {"response": "📞 Καλέστε στο 2610 450 000 για ταξί."}

    elif intent == "ServicesAndToursIntent":
        return {"response": "ℹ Για πληροφορίες εκδρομών, δείτε: www.example.com/tours"}

    elif intent == "PatrasInfoIntent":
        info = clients["patras"].get_info(user_message)
        return info or {"response": "ℹ Δεν βρέθηκαν σχετικές πληροφορίες για Πάτρα."}

    elif intent == "OnDutyPharmacyIntent":
        return clients["pharmacy"]._get()

    elif intent == "HospitalIntent":
        return clients["hospital"]._post()

    elif intent == "TripCostIntent":
        origin = entities.get("FROM")
        destination = entities.get("TO")

        # fallback if NER failed
        if not destination:
            fallback_entities = simple_location_extractor(user_message)
            origin = origin or fallback_entities.get("FROM", "Πάτρα")
            destination = fallback_entities.get("TO")

        if destination:
            return clients["timologio"]._post({"origin": origin, "destination": destination})
        else:
            return {"response": "❓ Δεν κατάλαβα τον προορισμό. Πού θέλεις να πας;"}

    # Fallback
    return {"response": "🤖 Δεν κατάλαβα ακριβώς. Θες να το ξαναπείς;"}

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
        return {"response": result["choices"][0]["message"]["content"]}
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return {"response": "⚠ Σφάλμα OpenAI. Προσπάθησε ξανά."}

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/healthz")
def healthz():
    return "OK"


def create_app():
    """Return the FastAPI application instance."""
    return app