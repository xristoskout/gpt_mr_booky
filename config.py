from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    openai_api_key: str
    pharmacy_api_url: str
    hospital_api_url: str
    timologio_api_url: str
    patras_llm_answers_api_url: str
    intents_path: str = "intents.json"
    cors_origins: str = "*"
