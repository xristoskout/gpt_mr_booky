# tests/test_session_store.py

from session_store import get_session, save_session, clear_session

def test_session_lifecycle():
    sid = "test123"
    # ensure clean
    clear_session(sid)
    s1 = get_session(sid)
    assert isinstance(s1, dict) and s1 == {}

    # save and retrieve
    s1["x"] = "y"
    save_session(sid, s1)
    s2 = get_session(sid)
    assert s2["x"] == "y"

    # clear again
    clear_session(sid)
    s3 = get_session(sid)
    assert s3 == {}
