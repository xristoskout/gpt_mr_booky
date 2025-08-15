# phrases.py
import json, os, random, time
from typing import List, Dict, Optional

_TRENDY: List[Dict] = []
_LAST_LOAD = 0.0
_FILE = os.getenv("TRENDY_PHRASES_FILE", "data/trendy_phrases.el.json")
_RELOAD_SEC = int(os.getenv("TRENDY_RELOAD_SEC", "300"))  # reload ανά 5'

def _load_trendy() -> List[Dict]:
    global _TRENDY, _LAST_LOAD
    try:
        mtime = os.path.getmtime(_FILE)
        if (not _TRENDY) or (mtime > _LAST_LOAD) or ((time.time() - _LAST_LOAD) > _RELOAD_SEC):
            with open(_FILE, "r", encoding="utf-8") as f:
                _TRENDY = json.load(f)
            _LAST_LOAD = mtime
    except Exception:
        pass
    return _TRENDY or []

def pick_trendy_phrase(*, emotion: Optional[str] = None, context: Optional[str] = None,
                       lang: str = "el", season: Optional[str] = None) -> Optional[str]:
    items = _load_trendy()
    if not items:
        return None

    def ok(x: Dict) -> bool:
        if lang and x.get("lang") not in (None, lang): return False
        if season and x.get("season") not in (None, "all", season): return False
        if emotion and (x.get("emotion") not in (emotion, "neutral")): return False
        if context:
            tags = set(x.get("context_tags") or [])
            if context not in tags and "generic" not in tags:
                return False
        if x.get("nsfw"): return False
        return True

    pool = [x for x in items if ok(x)]
    if not pool:
        return None
    weights = [float(x.get("weight", 1.0)) for x in pool]
    choice = random.choices(pool, weights=weights, k=1)[0]
    return choice.get("text")
