from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from the environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str

    # Optional AI lead scoring (Milestone 3). Never printed or exposed.
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    lead_scoring_model: str = "gpt-4o-mini"


@lru_cache
def get_settings() -> Settings:
    return Settings()
