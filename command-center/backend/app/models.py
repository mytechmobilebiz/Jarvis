"""ORM mappings for the tables created in migrations/*.sql.

The SQL files are the source of truth for the schema; these classes only map
it. tests/integration/test_schema.py checks the two stay in step.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, Computed, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True)
    display_name: Mapped[str] = mapped_column(Text, default="")
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now_col()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    can_access_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = _now_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = _now_col()
    user_agent: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = _now_col()
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text, default="")
    target_id: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ip: Mapped[str] = mapped_column(Text, default="")


class AgentState(Base):
    __tablename__ = "agent_state"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = _now_col()
    updated_by: Mapped[str] = mapped_column(Text, default="")


class SupervisorRequest(Base):
    __tablename__ = "supervisor_requests"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()
    source: Mapped[str] = mapped_column(Text, default="dashboard")
    requested_by: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="planning")
    planner: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    reply: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    summarizer: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    agent: Mapped[str] = mapped_column(Text)
    capability: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    cron: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()
    request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("supervisor_requests.id", ondelete="SET NULL")
    )
    schedule_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("schedules.id", ondelete="SET NULL"))
    agent: Mapped[str] = mapped_column(Text)
    capability: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    risk: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=1)
    run_after: Mapped[datetime] = _now_col()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str] = mapped_column(Text, default="")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(Text, default="api")
    created_by: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str] = mapped_column(Text, default="")


class TaskLog(Base):
    __tablename__ = "task_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"))
    at: Mapped[datetime] = _now_col()
    level: Mapped[str] = mapped_column(Text, default="info")
    message: Mapped[str] = mapped_column(Text)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _now_col()
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("tasks.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(Text, default="pending")
    agent: Mapped[str] = mapped_column(Text)
    capability: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    decided_by: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str] = mapped_column(Text, default="")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = _now_col()
    kind: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, default="info")
    source: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tasks.id", ondelete="SET NULL"))
    forward_status: Mapped[str] = mapped_column(Text, default="pending")
    forward_attempts: Mapped[int] = mapped_column(Integer, default=0)
    forward_error: Mapped[str] = mapped_column(Text, default="")
    forward_next_at: Mapped[datetime] = _now_col()
    forwarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    n8n_ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    n8n_execution_id: Mapped[str] = mapped_column(Text, default="")


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text, unique=True)
    client: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()
    last_status: Mapped[str] = mapped_column(Text, default="unknown")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_check_id: Mapped[int | None] = mapped_column(BigInteger)
    environment: Mapped[str] = mapped_column(Text, default="production")
    server_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("servers.id", ondelete="SET NULL"))
    server_site_key: Mapped[str] = mapped_column(Text, default="")
    last_diagnosis: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_diagnosis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SiteCheck(Base):
    __tablename__ = "site_checks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    site_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sites.id", ondelete="CASCADE"))
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tasks.id", ondelete="SET NULL"))
    checked_at: Mapped[datetime] = _now_col()
    status: Mapped[str] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    response_ms: Mapped[int | None] = mapped_column(Integer)
    final_url: Mapped[str] = mapped_column(Text, default="")
    tls_days_left: Mapped[int | None] = mapped_column(Integer)
    tls_issuer: Mapped[str] = mapped_column(Text, default="")
    is_wordpress: Mapped[bool] = mapped_column(Boolean, default=False)
    wp_version: Mapped[str] = mapped_column(Text, default="")
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tasks.id", ondelete="SET NULL"))
    agent: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, default="")
    content_type: Mapped[str] = mapped_column(Text, default="text/markdown")
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="draft")


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(Text, primary_key=True)
    started_at: Mapped[datetime] = _now_col()
    last_seen_at: Mapped[datetime] = _now_col()
    info: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class IntegrationStatus(Base):
    __tablename__ = "integration_status"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = _now_col()


class LLMUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = _now_col()
    provider: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = _now_col()
    updated_by: Mapped[str] = mapped_column(Text, default="")


class SecretValue(Base):
    __tablename__ = "secret_values"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    hint: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = _now_col()
    updated_by: Mapped[str] = mapped_column(Text, default="")


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()
    source: Mapped[str] = mapped_column(Text, default="dashboard")
    name: Mapped[str] = mapped_column(Text)
    company: Mapped[str] = mapped_column(Text, default="")
    email: Mapped[str] = mapped_column(Text, default="")
    phone: Mapped[str] = mapped_column(Text, default="")
    website: Mapped[str] = mapped_column(Text, default="")
    service_interest: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[int | None] = mapped_column(Integer)
    score_reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(Text, default="new")
    proposal_artifact_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("artifacts.id", ondelete="SET NULL"))


class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()
    source: Mapped[str] = mapped_column(Text, default="dashboard")
    customer_name: Mapped[str] = mapped_column(Text, default="")
    customer_email: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(Text, default="normal")
    sentiment: Mapped[str] = mapped_column(Text, default="")
    triage: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="open")
    reply_artifact_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("artifacts.id", ondelete="SET NULL"))


class OutboxEntry(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = _now_col()
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tasks.id", ondelete="SET NULL"))
    agent: Mapped[str] = mapped_column(Text)
    integration: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(Text, default="mock")
    action: Mapped[str] = mapped_column(Text)
    recipient: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    approved_by: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text, default="recorded, not sent")


class Server(Base):
    __tablename__ = "servers"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    host: Mapped[str] = mapped_column(Text)
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(Text)
    environment: Mapped[str] = mapped_column(Text, default="production")
    host_key: Mapped[str] = mapped_column(Text, default="")
    host_key_fingerprint: Mapped[str] = mapped_column(Text, default="")
    host_key_trusted_by: Mapped[str] = mapped_column(Text, default="")
    host_key_trusted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offered_host_key: Mapped[str] = mapped_column(Text, default="")
    offered_fingerprint: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    last_hello: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()


class ServerCommand(Base):
    __tablename__ = "server_commands"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    server_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("servers.id", ondelete="CASCADE"))
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("tasks.id", ondelete="SET NULL"))
    op: Mapped[str] = mapped_column(Text)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    kind: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(Text, default="")
    approved_by: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = _now_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    exit_status: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    refused: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str] = mapped_column(Text, default="")
    output_sha256: Mapped[str] = mapped_column(Text, default="")


# --- JARVIS Brain (0006_brain.sql) ---------------------------------------------------------------------------


def _search_col() -> Mapped[Any]:
    # Generated by PostgreSQL (see the migration); never written by the application.
    return mapped_column(TSVECTOR, Computed("search", persisted=True), deferred=True)


class ImportRun(Base):
    __tablename__ = "import_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_system: Mapped[str] = mapped_column(Text, default="jarvis")
    label: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(Text, default="running")
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    importer_version: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = _now_col()
    heartbeat_at: Mapped[datetime] = _now_col()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_by: Mapped[str] = mapped_column(Text, default="")


class ImportRecord(Base):
    __tablename__ = "import_records"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("import_runs.id", ondelete="CASCADE"))
    source_key: Mapped[str] = mapped_column(Text)
    record_type: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default="")
    target_table: Mapped[str] = mapped_column(Text, default="")
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    before: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = _now_col()


class MemoryItem(Base):
    __tablename__ = "memory_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    agent: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    sensitivity: Mapped[str] = mapped_column(Text, default="general")
    status: Mapped[str] = mapped_column(Text, default="pending_review")
    origin: Mapped[str] = mapped_column(Text, default="manual")
    flags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_system: Mapped[str] = mapped_column(Text, default="")
    source_ref: Mapped[str] = mapped_column(Text, default="")
    source_key: Mapped[str | None] = mapped_column(Text, unique=True)
    source_sha256: Mapped[str] = mapped_column(Text, default="")
    source_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    import_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("import_runs.id", ondelete="SET NULL"))
    content_hash: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _now_col()
    created_by: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = _now_col()
    updated_by: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[str] = mapped_column(Text, default="")
    search: Mapped[Any] = _search_col()


class MemoryItemVersion(Base):
    __tablename__ = "memory_item_versions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("memory_items.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    change: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    agent: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    sensitivity: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(Text, default="")
    changed_at: Mapped[datetime] = _now_col()
    note: Mapped[str] = mapped_column(Text, default="")


class ArchiveSession(Base):
    __tablename__ = "archive_sessions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_system: Mapped[str] = mapped_column(Text, default="jarvis")
    source_key: Mapped[str] = mapped_column(Text, unique=True)
    source_session_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    staff: Mapped[str] = mapped_column(Text, default="")
    agent: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(Text, default="chat")
    channel: Mapped[str] = mapped_column(Text, default="")
    lineage: Mapped[str] = mapped_column(Text, default="")
    parent_source_session_id: Mapped[str] = mapped_column(Text, default="")
    job_id: Mapped[str] = mapped_column(Text, default="")
    job_name: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str] = mapped_column(Text, default="")
    hermes_archived: Mapped[bool] = mapped_column(Boolean, default=False)
    source_message_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_messages: Mapped[int] = mapped_column(Integer, default=0)
    withheld_messages: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    sensitivity: Mapped[str] = mapped_column(Text, default="general")
    labels: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    flags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    import_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("import_runs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()
    search: Mapped[Any] = _search_col()


class ArchiveMessage(Base):
    __tablename__ = "archive_messages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("archive_sessions.id", ondelete="CASCADE"))
    source_key: Mapped[str] = mapped_column(Text, unique=True)
    source_message_id: Mapped[str] = mapped_column(Text)
    source_fp: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, default="")
    content_chars: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    tool_name: Mapped[str] = mapped_column(Text, default="")
    tool_call_id: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(Text, default="live")
    sensitivity: Mapped[str] = mapped_column(Text, default="general")
    withheld: Mapped[bool] = mapped_column(Boolean, default=False)
    redactions: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    labels: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    flags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    import_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("import_runs.id", ondelete="SET NULL"))
    source_uid: Mapped[str] = mapped_column(Text, default="")  # 0008: the JARVIS message_uid ('' unknown)
    # 0008: the uids a Hermes fold merged into this row (absorbed_message_uids): it holds their text
    absorbed_uids: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    search: Mapped[Any] = _search_col()


class MemoryConflict(Base):
    __tablename__ = "memory_conflicts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("import_runs.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    source_key: Mapped[str] = mapped_column(Text, default="")
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    existing_table: Mapped[str] = mapped_column(Text, default="")
    existing_id: Mapped[int | None] = mapped_column(BigInteger)
    incoming_item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("memory_items.id", ondelete="CASCADE"))
    existing: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    incoming: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(Text, default="open")
    effects: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    decided_by: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = _now_col()


class WorkflowDraft(Base):
    __tablename__ = "workflow_drafts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_system: Mapped[str] = mapped_column(Text, default="jarvis")
    source_key: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    staff: Mapped[str] = mapped_column(Text, default="")
    agent: Mapped[str] = mapped_column(Text, default="")
    capability: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    compatibility: Mapped[str] = mapped_column(Text)
    trigger: Mapped[str] = mapped_column(Text, default="schedule")
    cron: Mapped[str] = mapped_column(Text, default="")
    schedule_display: Mapped[str] = mapped_column(Text, default="")
    timezone: Mapped[str] = mapped_column(Text, default="")
    enabled_in_source: Mapped[bool] = mapped_column(Boolean, default=False)
    deliver: Mapped[str] = mapped_column(Text, default="")
    approval: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error_handling: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    duplicate_guard: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    spec: Mapped[str] = mapped_column(Text, default="")
    history: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    notes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(Text, default="draft")
    schedule_id: Mapped[int | None] = mapped_column(BigInteger)  # not a foreign key: see 0006_brain.sql
    source_fp: Mapped[str] = mapped_column(Text, default="")
    import_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("import_runs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _now_col()
    updated_at: Mapped[datetime] = _now_col()
    reviewed_by: Mapped[str] = mapped_column(Text, default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrainTombstone(Base):
    __tablename__ = "brain_tombstones"
    source_key: Mapped[str] = mapped_column(Text, primary_key=True)
    purged_at: Mapped[datetime] = _now_col()
    purged_by: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    # 0008: a withhold's "stone", the identity of the JARVIS message (app/brain/identity.py). Purges leave them empty.
    source_session_id: Mapped[str] = mapped_column(Text, default="")
    source_row_id: Mapped[str] = mapped_column(Text, default="")
    role: Mapped[str] = mapped_column(Text, default="")
    tool_call_id: Mapped[str] = mapped_column(Text, default="")
    message_uid: Mapped[str] = mapped_column(Text, default="")
    uid_known: Mapped[bool] = mapped_column(Boolean, default=False)
    anchor_key: Mapped[str] = mapped_column(Text, default="")
    via: Mapped[str] = mapped_column(Text, default="")
    review: Mapped[str] = mapped_column(Text, default="")
    bound: Mapped[bool] = mapped_column(Boolean, default=False)
    after_run: Mapped[int] = mapped_column(BigInteger, default=0)
    # 0008: the admin hid the conversation's title with this withhold (an admin's own stone only)
    title_hidden: Mapped[bool] = mapped_column(Boolean, default=False)


class BrainWithholdExemption(Base):
    """An admin's release of a row held for review: the uid and same-text rules no longer hold it for that group."""

    __tablename__ = "brain_withhold_exemptions"
    anchor_key: Mapped[str] = mapped_column(Text, primary_key=True)
    source_session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_row_id: Mapped[str] = mapped_column(Text, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, default="released by an admin")
    decided_by: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime] = _now_col()
    # The content the release covers (identity.Binding): the released row's uid, length (-1 unknown) and, once it is
    # readable, the keyed digest of its stored readable text (identity.release_fp; '' unknown).
    source_uid: Mapped[str] = mapped_column(Text, default="")
    content_chars: Mapped[int] = mapped_column(Integer, default=-1)
    content_fp: Mapped[str] = mapped_column(Text, default="")


class BrainPurgedContent(Base):
    """The keyed fingerprint (identity.purge_fp) of a purged item's bytes: never a plain hash."""

    __tablename__ = "brain_purged_content"
    fp: Mapped[str] = mapped_column(Text, primary_key=True)
    source_key: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(Text, default="")
    purged_at: Mapped[datetime] = _now_col()
    purged_by: Mapped[str] = mapped_column(Text, default="")
    via: Mapped[str] = mapped_column(Text, default="")


class BrainPurgedItem(Base):
    """Every purge, with or without a source key: the item's id and creation time (no text, no hash), so that
    decisions-apply --removals-only purges the very same item again when a restore brought it back (0008)."""

    __tablename__ = "brain_purged_items"
    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    origin: Mapped[str] = mapped_column(Text, default="")
    replaces_source_key: Mapped[str] = mapped_column(Text, default="")
    purged_at: Mapped[datetime] = _now_col()
    purged_by: Mapped[str] = mapped_column(Text, default="")


class BrainMaintenance(Base):
    """One-time brain steps that are done (identity repair, purge identity)."""

    __tablename__ = "brain_maintenance"
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    done_at: Mapped[datetime] = _now_col()
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
