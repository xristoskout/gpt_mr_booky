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
<<<<<<< HEAD

from config import Settings
from api_clients import build_clients
from intents import IntentClassifier
from constants import SYSTEM_PROMPT

=======
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

>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8
# === Init Logger ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Load NLP ===
<<<<<<< HEAD
# Προσπαθούμε να φορτώσουμε το ελληνικό μοντέλο, αλλιώς προεπιλογή στα αγγλικά
=======
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8
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
<<<<<<< HEAD
    pattern = r"απο\s+(?P<from>\w+).*?(?:μεχρι|μέχρι|προς|για|εως)\s+(?P<to>\w+)"
=======
    # pattern με ονομαστικά groups
    pattern = r"απο\s+(?P<from>\w+).*?(?:μεχρι|προς|για|εως|μέχρι)\s+(?P<to>\w+)"
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8
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
<<<<<<< HEAD
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
=======
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
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8

    # Εκδρομές/υπηρεσίες
    if intent == "ServicesAndToursIntent":
        tours = kb.get("services_and_tours", {})
        if tours:
<<<<<<< HEAD
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
=======
            return {"reply": funny_services_response(
                tours.get("summary", ""),
                tours.get("services", {}),
                tours.get("tours", []),
            )}
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8

    # Πατρα LLM Answers (generic info)
    if intent == "PatrasLlmAnswersIntent":
        try:
            resp = clients["patras-llm-answers"].answer(user_message)
<<<<<<< HEAD
            # resp μπορεί να είναι dict ή string
            if isinstance(resp, dict):
                base_answer = resp.get("answer") or resp.get("reply") or json.dumps(resp, ensure_ascii=False)
            else:
                base_answer = resp
            reply = ask_llm_with_system_prompt(user_message, base_answer)
            return {"reply": reply}
=======
            return {"reply": funny_patras_response(resp)}
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8
        except Exception as e:
            logger.error(f"Patras LLM Answers client error: {e}")
            return {"reply": "❌ Σφάλμα αναζήτησης πληροφοριών."}

<<<<<<< HEAD
    # Εφημερεύοντα φαρμακεία
    if intent == "OnDutyPharmacyIntent":
=======
    elif intent == "OnDutyPharmacyIntent":
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8
        area = entities.get("AREA", "Πάτρα")
        try:
            response = clients["pharmacy"].get_on_duty(area=area, method="get")
            pharmacies = response.get("pharmacies", [])
<<<<<<< HEAD
            if pharmacies:
                context = "\n".join([f"{p['name']} – {p['address']} ({p['time_range']})" for p in pharmacies])
            else:
                context = f"Δεν βρέθηκαν εφημερεύοντα φαρμακεία στην περιοχή {area}."
            reply = ask_llm_with_system_prompt(user_message, context)
            return {"reply": reply}
=======
            return {"reply": funny_pharmacy_response(pharmacies)}
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8
        except Exception as e:
            logger.error(f"Pharmacy client error: {e}")
            return {"reply": "❌ Σφάλμα συστήματος φαρμακείων."}

    # Εφημερεύοντα νοσοκομεία
    if intent == "HospitalIntent":
        try:
            resp = clients["hospital"].info()
<<<<<<< HEAD
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
=======
            # αν η απάντηση είναι string, προσθέτουμε απλώς χιουμοριστικό επίλογο
            if isinstance(resp, str):
                return {"reply": resp + "\n💉 Μην ξεχνάς να φοράς ζώνη ασφαλείας!"}
            # αν επιστραφεί δομημένο dict:
            hospitals = resp.get("hospitals", [])
            on_call_msg = resp.get("on_call_message", "")
            return {"reply": funny_hospital_response(hospitals, on_call_msg)}
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8
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
<<<<<<< HEAD
                base_text = f"Απόσταση {origin}–{destination}: κόστος {cost_info['cost']}€"
                reply = ask_llm_with_system_prompt(user_message, base_text)
                return {"reply": reply}
=======
                # χρησιμοποιούμε το χιουμοριστικό trip_response
                return {"reply": funny_trip_response(origin, destination, cost_info['cost'])}
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8
            else:
                try:
                    # καταγράφουμε τι στέλνουμε στο timologio για debugging
                    logger.debug(f"Timologio request: origin={origin}, destination={destination}")
                    result = clients["timologio"].calculate({"origin": origin, "destination": destination})
<<<<<<< HEAD
                    # Προσπαθούμε να βρούμε τιμή στο αποτέλεσμα
=======
                    # αν το API μας επιστρέψει το κόστος, το περνάμε στη χιουμοριστική απόκριση
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8
                    price = None
                    for key in ("total_fare", "total_cost", "fare"):
                        if key in result:
                            price = result[key]
                            break
                    if price is not None:
<<<<<<< HEAD
                        context = f"Κόστος {origin}–{destination}: {price}€"
                    else:
                        # Αν δεν υπάρχει τιμή, χρησιμοποιούμε όλο το JSON
                        context = json.dumps(result, ensure_ascii=False)
                    reply = ask_llm_with_system_prompt(user_message, context)
                    return {"reply": reply}
=======
                        return {"reply": funny_trip_response(origin, destination, price)}
                    return {"reply": funny_patras_response(result)}
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8
                except Exception as e:
                    logger.error(f"TripCost client error: {e}")
                    return {"reply": "❌ Σφάλμα υπολογισμού κόστους."}
        # Χρειάζεται προορισμός
        return {"reply": ask_llm_with_system_prompt(user_message, "Δεν δόθηκε προορισμός.")}

<<<<<<< HEAD
    # Αν δεν αναγνωρίστηκε πρόθεση – fallback
    unknown_reply = ask_llm_with_system_prompt(user_message, "Δεν αναγνωρίζω αυτήν την ερώτηση.")
    return {"reply": unknown_reply}
=======
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
>>>>>>> 1727f05ffa9b227bfcfe3ba41d5a28c0bb136cc8

# === Health Endpoints ===
@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/healthz")
def healthz():
    return Response("OK", media_type="text/plain")

def create_app():
    return app
