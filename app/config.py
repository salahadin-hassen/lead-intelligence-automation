from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from the environment / .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str

    llm_provider: Literal["openrouter", "openai-compatible"] = "openrouter"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str = "openrouter/free"
    app_env: str = "development"
    render: bool = False
    api_auth_token: str | None = None

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in ("production", "prod") or self.render


@lru_cache
def get_settings() -> Settings:
    return Settings()
