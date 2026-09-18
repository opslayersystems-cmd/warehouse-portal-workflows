from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./warehouse_portal.db"
    openai_api_key: str = ""
    openai_research_model: str = ""
    openai_fast_model: str = ""
    google_maps_api_key: str = ""
    google_discovery_monthly_limit: int = Field(default=1000, ge=0, le=1000)
    google_allowed_email: str = ""
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = ""
    apollo_api_key: str = ""
    send_enabled: bool = False
    session_secret_key: str = ""
    session_https_only: bool = False

    @field_validator("session_secret_key")
    @classmethod
    def long_session_secret(cls, value: str) -> str:
        if value and len(value.encode("utf-8")) < 32:
            raise ValueError("SESSION_SECRET_KEY must contain at least 32 bytes")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
