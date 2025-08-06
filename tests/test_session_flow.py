import os
from fastapi.testclient import TestClient

# Ensure required settings are present before importing main
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("PHARMACY_API_URL", "http://test")
os.environ.setdefault("HOSPITAL_API_URL", "http://test")
os.environ.setdefault("TIMOLOGIO_API_URL", "http://test")
os.environ.setdefault("PATRAS_LLM_ANSWERS_API_URL", "http://test")

import main


def test_last_intent_reset_after_completion():
    client = TestClient(main.app)
    user_id = "u1"

    # Simulate completed TripCostIntent conversation
    main.sess_mgr.add_history(user_id, "TripCostIntent", "user", "bot")
    main.sess_mgr.set_active_intent(user_id, None)

    # New question should not see old intent or confirmation
    r = client.post("/chat", json={"message": "Τι κάνεις;", "user_id": user_id})
    data = r.json()
    assert main.sess_mgr.get_last_intent(user_id) is None
    assert not data.get("ask_confirm")