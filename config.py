# file: config.py
from __future__ import annotations

from typing import List, Optional, Union

# Prefer Pydantic v2 (pydantic-settings). Fallback to v1 if not installed.
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore
    from pydantic import field_validator  # type: ignore

    class Settings(BaseSettings):
        ENV: str = "dev"
        # Accept comma-separated string, JSON array, list or empty
        ALLOWED_ORIGINS: Union[str, List[str], None] = None
        ALLOWED_ORIGIN_REGEX: Optional[str] = None

        PERSIST_BACKEND: str = "memory"  # memory|redis
        REDIS_URL: Optional[str] = None

        TOOL_TIMEOUT_SEC: int = 25
        MAX_BODY_BYTES: int = 1_000_000
        MAX_MESSAGE_CHARS: int = 2000

        RATE_LIMIT_WINDOW_SEC: int = 60
        RATE_LIMIT_MAX_REQ: int = 60

        # Important: ignore extra env vars (TRENDY_PROB, OPENAI_API_KEY, etc.)
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            case_sensitive=False,
        )

        @field_validator("ALLOWED_ORIGINS", mode="before")
        @classmethod
        def _parse_origins(cls, v):
            if v is None or v == "":
                return []
            if isinstance(v, list):
                return v
            if isinstance(v, str):
                s = v.strip()
                if s.startswith("[") and s.endswith("]"):
                    try:
                        import json
                        parsed = json.loads(s)
                        if isinstance(parsed, list):
                            return [str(x).strip() for x in parsed if str(x).strip()]
                    except Exception:
                        pass
                return [s2.strip() for s2 in s.split(",") if s2.strip()]
            return v

except Exception:
    # Pydantic v1 fallback
    from pydantic import BaseSettings, validator

    class Settings(BaseSettings):
        ENV: str = "dev"
        ALLOWED_ORIGINS: List[str] = []
        ALLOWED_ORIGIN_REGEX: Optional[str] = None

        PERSIST_BACKEND: str = "memory"
        REDIS_URL: Optional[str] = None

        TOOL_TIMEOUT_SEC: int = 25
        MAX_BODY_BYTES: int = 1_000_000
        MAX_MESSAGE_CHARS: int = 2000

        RATE_LIMIT_WINDOW_SEC: int = 60
        RATE_LIMIT_MAX_REQ: int = 60

        @validator("ALLOWED_ORIGINS", pre=True)
        def _parse_origins(cls, v):
            if v is None or v == "":
                return []
            if isinstance(v, list):
                return v
            if isinstance(v, str):
                s = v.strip()
                if s.startswith("[") and s.endswith("]"):
                    try:
                        import json
                        parsed = json.loads(s)
                        if isinstance(parsed, list):
                            return [str(x).strip() for x in parsed if str(x).strip()]
                    except Exception:
                        pass
                return [s2.strip() for s2 in s.split(",") if s2.strip()]
            return v

        class Config:
            env_file = ".env"
            env_file_encoding = "utf-8"
            case_sensitive = False
            extra = "ignore"
