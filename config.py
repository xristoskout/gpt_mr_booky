from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import HttpUrl, EmailStr, Field

class Settings(BaseSettings):
    openai_api_key: str = Field(..., env="OPENAI_API_KEY")
    pharmacy_api_url: str = Field(..., env="PHARMACY_API_URL")
    hospital_api_url: str = Field(..., env="HOSPITAL_API_URL")
    timologio_api_url: str = Field(..., env="TIMOLOGIO_API_URL")
    patras_llm_answers_api_url: str = Field(..., env="PATRAS_LLM_ANSWERS_API_URL")
    service_bearer_token: str | None = Field(default=None, env="SERVICE_BEARER_TOKEN")

    taxi_express_phone: str | None = None
    taxi_website_url: HttpUrl | None = None
    taxi_app_url: HttpUrl | str | None = None
    taxi_booking_url: HttpUrl | str | None = None
    taxi_email: EmailStr | str | None = None

    # Αν στο .env έχεις λίστα CORs τύπου: http://localhost:3000,https://taxipatras.com
    # κράτα το ως string και κάνε split αλλού (π.χ. στο main) πριν το περάσεις στο CORSMiddleware.
    cors_origins: str = Field(default="*")

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",  # βάλε "forbid" αν θέλεις αυστηρότητα και έχεις ΜΟΝΟ δηλωμένα κλειδιά
    )