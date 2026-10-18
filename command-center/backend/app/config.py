"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    app_name: str = "Infinite AI Command Center"
    environment: Literal["development", "test", "production"] = "development"

    # Database
    database_url: str = "postgresql+asyncpg://command_center:command_center@localhost:5432/command_center"
    db_pool_size: int = 10

    # Auth
    admin_email: str = "admin@example.com"
    admin_password: SecretStr | None = None
    service_token: SecretStr | None = Field(default=None, validation_alias="CC_SERVICE_TOKEN")
    session_ttl_hours: int = 12
    cookie_secure: bool = False
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    # AI provider. "auto" uses Claude when ANTHROPIC_API_KEY is set, else the
    # deterministic rule-based planner.
    llm_provider: Literal["auto", "anthropic", "rule_based"] = "auto"
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_timeout_seconds: float = 90.0
    llm_daily_request_cap: int = 200

    # n8n
    n8n_base_url: str = "http://n8n:5678"
    n8n_events_webhook_path: str = "cc-events"
    n8n_webhook_secret: SecretStr | None = None
    n8n_api_key: SecretStr | None = None
    n8n_forward_min_severity: Literal["info", "warning", "critical"] = "warning"

    # Worker
    worker_concurrency: int = 4
    worker_poll_seconds: float = 2.0
    task_timeout_seconds: int = 180
    stale_task_minutes: int = 10

    # Monitoring agents (read-only HTTP)
    monitor_user_agent: str = "InfiniteAI-CommandCenter-Monitor/1.0 (read-only health check)"
    monitor_timeout_seconds: float = 20.0
    monitor_max_body_bytes: int = 600_000
    monitor_slow_ms: int = 3000
    monitor_retry_delay_seconds: float = 2.0
    # Hostnames that may resolve to private/loopback addresses (e.g. a local
    # test WordPress). Everything else that resolves privately is refused, so a
    # monitored URL can never be used to reach internal services.
    monitor_allowed_private_hosts: str = ""
    wp_core_version_check: bool = True
    wp_version_api_url: str = "https://api.wordpress.org/core/version-check/1.7/"

    @property
    def allowed_private_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.monitor_allowed_private_hosts.split(",") if h.strip()}

    @property
    def anthropic_enabled(self) -> bool:
        if self.llm_provider == "rule_based":
            return False
        return bool(self.anthropic_api_key and self.anthropic_api_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
