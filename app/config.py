import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Gemini
    google_api_key: str = ""
    chat_model: str = "gemini-3.8-flash"
    # Comma-separated models to try, in order, when the main one fails (e.g. 503 "high demand").
    fallback_models: str = ""
    # How much the model reasons before answering (minimal/low/medium/high). Higher is slower and can
    # use up the output budget before any answer text is written.
    thinking_level: str = "low"
    embedding_model: str = "gemini-embedding-001"
    # Firestore vector indexes support at most 2048 dims; 768 is plenty here.
    embedding_dim: int = 768

    # Firestore (Firebase Admin service account JSON, as a single-line string)
    firebase_service_account_json: str = ""
    firebase_project_id: str = ""
    chunks_collection: str = "rag_chunks"

    # Retrieval
    top_k: int = 6

    # API
    allowed_origins: str = "http://localhost:5173"
    rate_limit: str = "20/minute"
    max_message_chars: int = 1000
    max_history_messages: int = 10
    admin_token: str = ""  # protects POST /admin/reindex; the endpoint is disabled when empty

    # Who the bot is speaking for
    owner_name: str = "Salil Vartak"
    site_url: str = "https://vartak.online"  # used in the prompt and for blog-post links in citations
    contact_email: str = ""  # where the bot sends visitors when it can't answer

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def service_account_info(self) -> dict | None:
        if not self.firebase_service_account_json:
            return None
        return json.loads(self.firebase_service_account_json)


@lru_cache
def get_settings() -> Settings:
    return Settings()
