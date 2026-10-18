"""ORM mappings for the tables created in migrations/*.sql.

The SQL files are the source of truth for the schema; these classes only map
it. tests/integration/test_schema.py checks the two stay in step.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
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
