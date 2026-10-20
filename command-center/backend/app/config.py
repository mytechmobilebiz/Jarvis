"""Runtime configuration, read from environment variables (see .env.example)."""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SESSION_COOKIE_DEFAULT = "cc_session"
# The Research Gateway's search service: the private SearXNG container, on the worker's "research" network only. The
# address is fixed (check-config refuses any other SEARXNG_URL, and the gateway refuses to search with one), so a
# query can never go to a public SearXNG. Monitoring may never treat these hosts as allowed private hosts.
SEARXNG_URL = "http://searxng:8080"
RESEARCH_HOSTS = frozenset({"searxng", "research-fetch"})
# A cookie name is an HTTP token; this is a safe subset of one (no separators, quotes, spaces or controls).
COOKIE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
INSTANCE_LABEL = re.compile(r"[\x20-\x7e]*")


class Settings(BaseSettings):
    """Deployment and developer settings, read from the environment (.env).

    These describe the installation itself (database, secrets, ports, tuning)
    and are never shown or edited in the dashboard. Settings a customer may
    change live in the database instead: see app/customer_settings.py.
    """

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
    # The sign-in cookie's name. Browsers share cookies between ports of one host, so a second installation on
    # this PC (the JARVIS Brain test copy on 127.0.0.1:8190) uses its own name and never replaces, or is signed in
    # by, the live installation's cookie. The default keeps the live installation as it was.
    session_cookie_name: str = SESSION_COOKIE_DEFAULT
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    # A label for this installation, shown as a banner on every dashboard page and returned by
    # /api/system/status (the JARVIS Brain test copy sets "cc-brain-test"). Empty for the live installation.
    instance_label: str = Field(default="", validation_alias="CC_INSTANCE_LABEL")

    # Encrypts customer secrets stored in the database (app/secrets.py).
    secrets_key: SecretStr | None = Field(default=None, validation_alias="CC_SECRETS_KEY")

    # Edition when no valid license is installed (developer setting). The
    # internal "developer" edition enables everything and is not for sale.
    edition: str = Field(default="developer", validation_alias="CC_EDITION")
    # Ed25519 public key (base64, 32 bytes) that signs license files. Without
    # it, uploaded licenses cannot be verified and are ignored.
    license_public_key: str = Field(default="", validation_alias="CC_LICENSE_PUBLIC_KEY")

    # Credit Repair module: its own database (own login role) and its own
    # field-encryption key, never shared with the rest of the platform.
    credit_repair_database_url: str = ""
    credit_repair_data_key: SecretStr | None = None

    # Workflow engine the platform hands events to: n8n, a generic signed
    # webhook (any engine), or none.
    workflow_engine: Literal["n8n", "webhook", "none"] = "n8n"
    workflow_webhook_url: str = ""
    workflow_webhook_secret: SecretStr | None = None

    # AI provider defaults. Customers override these (and supply their own API
    # key) on the Settings page. "auto" uses Claude when a key is available,
    # else the deterministic rule-based planner.
    llm_provider: Literal["auto", "anthropic", "rule_based"] = "auto"
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_timeout_seconds: float = 90.0
    llm_daily_request_cap: int = 200

    # n8n
    n8n_base_url: str = "http://n8n:5678"
    # Where a person opens the n8n editor in their browser (the dashboard's "Open n8n" link).
    n8n_editor_url: str = "http://localhost:5680"
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
    # Comma-separated URLs whose answer shows the Command Center is online (the control check before a site
    # is recorded as down); they replace www.cloudflare.com and www.google.com. The WordPress.org API is always tried.
    monitor_control_urls: str = ""
    # SSH to managed servers (through cc-gate only). Private addresses are refused
    # unless listed here (the disposable test server, for example).
    ssh_allowed_private_hosts: str = ""
    ssh_connect_timeout_seconds: float = 15.0
    # Longer than the gate's own budget per operation (120 s / 600 s by default, plus
    # its stop grace period), so the gate normally answers "timed out" itself.
    ssh_command_timeout_seconds: int = 150
    ssh_slow_command_timeout_seconds: int = 660
    wp_core_version_check: bool = True
    wp_version_api_url: str = "https://api.wordpress.org/core/version-check/1.7/"

    # Research Gateway (app/research): fixed, see SEARXNG_URL. Whether research is on is not a deployment setting:
    # an admin switches it in the Research Center (off by default).
    searxng_url: str = SEARXNG_URL

    @field_validator("session_cookie_name")
    @classmethod
    def _cookie_name_is_a_token(cls, value: str) -> str:
        if not COOKIE_NAME.fullmatch(value):
            raise ValueError("SESSION_COOKIE_NAME must be 1-64 letters, digits, '_' or '-' (for example cc_session)")
        return value

    @field_validator("instance_label")
    @classmethod
    def _label_is_one_short_line(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 64 or not INSTANCE_LABEL.fullmatch(value):
            raise ValueError("CC_INSTANCE_LABEL must be at most 64 printable ASCII characters")
        return value

    @property
    def allowed_private_hosts(self) -> set[str]:
        """MONITOR_ALLOWED_PRIVATE_HOSTS, never with the research containers: monitoring's check_url (which n8n can
        start) must not reach SearXNG without the research screen."""
        hosts = {h.strip().lower() for h in self.monitor_allowed_private_hosts.split(",") if h.strip()}
        return {h for h in hosts if h.rstrip(".") not in RESEARCH_HOSTS}

    @property
    def ssh_private_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.ssh_allowed_private_hosts.split(",") if h.strip()}

    @property
    def anthropic_enabled(self) -> bool:
        if self.llm_provider == "rule_based":
            return False
        return bool(self.anthropic_api_key and self.anthropic_api_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
