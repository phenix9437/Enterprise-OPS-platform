"""Central configuration. All values come from environment variables / .env."""
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INSECURE_DEFAULT_SECRET = "change-me-in-production-change-me-in-production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Enterprise Ops Platform"
    environment: str = "development"  # development | test | production
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/enterprise_ops"

    jwt_secret: str = _INSECURE_DEFAULT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_days: int = 7
    bcrypt_rounds: int = 12                # lowered only in tests for speed

    # --- LLM (Phase 2) ---
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"      # any model that supports structured outputs
    llm_timeout_seconds: float = 60.0
    llm_sdk_max_retries: int = 2           # SDK-level retries for 429/5xx/network errors
    planner_max_attempts: int = 2          # re-asks when the plan fails OUR validation
    planner_max_steps: int = 12
    chat_history_messages: int = 10        # how many past messages the planner sees

    @model_validator(mode="after")
    def _block_insecure_secret_in_prod(self) -> "Settings":
        if self.environment == "production" and self.jwt_secret == _INSECURE_DEFAULT_SECRET:
            raise ValueError("JWT_SECRET must be set to a strong value in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
