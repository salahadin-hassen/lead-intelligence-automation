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
    # Optional AI lead insights (app/insights.py). Separate from LLM_API_KEY on
    # purpose: setting LLM_API_KEY makes the LLM replace the deterministic score.
    ai_insights_enabled: bool = False
    openai_api_key: str | None = None
    ai_insights_model: str = "gpt-6-luna"
    ai_insights_reasoning_effort: str = "none"
    ai_insights_base_url: str = "https://api.openai.com/v1"
    ai_insights_timeout_seconds: float = 15.0
    ai_insights_max_output_tokens: int = 300
    ai_insights_max_input_chars: int = 2000
    # Global ceiling on paid insights calls (all callers together; the public
    # webhook gives no trustworthy per-client identity). 0 disables calls.
    ai_insights_max_calls_per_hour: int = 20
    ai_insights_max_calls_per_day: int = 100
    app_env: str = "development"
    render: bool = False
    api_auth_token: str | None = None
    # Cloudflare Turnstile verification for POST /leads (app/turnstile.py).
    # Enforced by default. TURNSTILE_ENABLED=false is honoured only outside
    # production (local development, the test suite); production always
    # verifies, and a missing secret there fails closed with HTTP 503.
    turnstile_enabled: bool = True
    turnstile_secret_key: str | None = None
    # Comma-separated, matched exactly against Siteverify's "hostname".
    turnstile_allowed_hostnames: str = ""
    turnstile_expected_action: str = "lead-intake"
    turnstile_timeout_seconds: float = 5.0

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in ("production", "prod") or self.render

    @property
    def turnstile_required(self) -> bool:
        return self.is_production or self.turnstile_enabled

    @property
    def turnstile_hostnames(self) -> frozenset[str]:
        return frozenset(h.strip() for h in self.turnstile_allowed_hostnames.split(",") if h.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
