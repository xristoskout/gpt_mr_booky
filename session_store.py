# session_store.py
import threading
from typing import Dict

_sessions: Dict[str, Dict] = {}
_lock = threading.Lock()

def get_session(sid: str) -> Dict:
    with _lock:
        return _sessions.setdefault(sid, {})

def save_session(sid: str, sess: Dict) -> None:
    with _lock:
        _sessions[sid] = sess

def clear_session(sid: str) -> None:
    with _lock:
        _sessions.pop(sid, None)
