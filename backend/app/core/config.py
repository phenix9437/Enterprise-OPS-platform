
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

    # --- Tool layer / MCP (Phase 3) ---
    tools_enabled: bool = True             # start the MCP tool servers with the API
    tool_call_timeout_seconds: float = 30.0
    tool_max_retries: int = 2              # only for read-only / idempotent tools
    tool_retry_backoff_seconds: float = 0.3
    tools_disabled: str = ""               # comma-separated registry names, e.g. "gmail.send_email"

    # --- Agent execution (Phase 4) ---
    step_max_attempts: int = 2             # tries per plan step (1 initial + retries with feedback)
    executor_max_calls_per_step: int = 10  # fan-out cap, e.g. one call per customer
    context_chars_per_item: int = 8000     # how much of each earlier step's output the LLM sees
    checkpoint_database_url: str = ""      # defaults to DATABASE_URL (PostgreSQL -> durable checkpoints)

    # --- Approvals (Phase 5) ---
    approval_default_expiry_minutes: int = 1440   # unanswered requests expire after this long
    approval_allow_self_approval: bool = False    # True only for solo development (one person = requester AND approver)

    # --- Workflows, scheduling, Celery (Phase 6) ---
    celery_enabled: bool = False           # True: tasks run in Celery workers instead of inside the API process
    redis_url: str = "redis://localhost:6379/0"
    worker_lease_seconds: int = 90         # a worker must renew its claim on a task within this time
    worker_stale_grace_seconds: int = 120  # how long a 'running' task may sit unclaimed before it is re-dispatched
    schedule_tick_seconds: int = 30        # how often beat checks for due schedules
    schedule_min_interval_minutes: int = 5 # refuse schedules that would fire more often than this
    workflow_max_calls_per_step: int = 100 # fan-out cap for code-built steps

    # business data served by the business_db MCP server (defaults to the app DB for the demo)
    business_database_url: str = ""
    seed_demo_data: bool = False
    gmail_backend: str = "mock"            # mock | google
    drive_backend: str = "mock"            # mock | google
    google_credentials_file: str = ""      # authorized-user JSON for the google backends
    gmail_allowed_recipient_domains: str = ""  # comma-separated; empty = any domain

    @model_validator(mode="after")
    def _validate_jwt_secret(self) -> "Settings":
        if "jwt_secret" not in self.model_fields_set:
            if self.environment == "production":
                raise ValueError("JWT_SECRET must be explicitly configured in production")
            return self

        secret = self.jwt_secret.strip()
        forbidden = {
            _INSECURE_DEFAULT_SECRET.casefold(),
            "change-me",
            "changeme",
            "your-secret",
            "your-secret-key",
            "replace-me",
            "password",
            "secret",
        }

        if len(secret.encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET must contain at least 32 bytes")

        if secret.casefold() in forbidden:
            raise ValueError("JWT_SECRET must not be a default or placeholder value")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
