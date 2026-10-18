-- Core schema: users and sessions, audit trail, agents, supervisor requests,
-- the task queue, approvals, schedules, events, website monitoring, artifacts.

CREATE TABLE users (
    id              BIGSERIAL PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL DEFAULT '',
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('admin', 'operator', 'viewer')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMPTZ
);

CREATE TABLE sessions (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_agent      TEXT NOT NULL DEFAULT '',
    ip              TEXT NOT NULL DEFAULT ''
);
CREATE INDEX sessions_user_idx ON sessions (user_id);

CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_type     TEXT NOT NULL DEFAULT '',
    target_id       TEXT NOT NULL DEFAULT '',
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip              TEXT NOT NULL DEFAULT ''
);
CREATE INDEX audit_log_at_idx ON audit_log (at DESC);

-- Runtime state per agent module. The modules themselves are code; this row
-- only records whether the operator has switched one off.
CREATE TABLE agent_state (
    name            TEXT PRIMARY KEY,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE supervisor_requests (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source          TEXT NOT NULL DEFAULT 'dashboard',
    requested_by    TEXT NOT NULL DEFAULT '',
    message         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'planning'
                    CHECK (status IN ('planning', 'running', 'awaiting_approval', 'completed', 'failed')),
    planner         TEXT NOT NULL DEFAULT '',
    plan            JSONB NOT NULL DEFAULT '{}'::jsonb,
    reply           TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    summarizer      TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    completed_at    TIMESTAMPTZ
);
CREATE INDEX supervisor_requests_created_idx ON supervisor_requests (created_at DESC);

CREATE TABLE schedules (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT NOT NULL DEFAULT '',
    agent           TEXT NOT NULL,
    capability      TEXT NOT NULL,
    params          JSONB NOT NULL DEFAULT '{}'::jsonb,
    cron            TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at     TIMESTAMPTZ,
    next_run_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tasks (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id      BIGINT REFERENCES supervisor_requests(id) ON DELETE SET NULL,
    schedule_id     BIGINT REFERENCES schedules(id) ON DELETE SET NULL,
    agent           TEXT NOT NULL,
    capability      TEXT NOT NULL,
    params          JSONB NOT NULL DEFAULT '{}'::jsonb,
    risk            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'succeeded', 'failed',
                                      'awaiting_approval', 'rejected', 'cancelled')),
    priority        INTEGER NOT NULL DEFAULT 100,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 1,
    run_after       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    locked_by       TEXT NOT NULL DEFAULT '',
    heartbeat_at    TIMESTAMPTZ,
    source          TEXT NOT NULL DEFAULT 'api',
    created_by      TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    result          JSONB,
    error           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX tasks_queue_idx ON tasks (priority, id) WHERE status = 'queued';
CREATE INDEX tasks_request_idx ON tasks (request_id);
CREATE INDEX tasks_created_idx ON tasks (created_at DESC);
CREATE INDEX tasks_agent_idx ON tasks (agent, created_at DESC);

CREATE TABLE task_logs (
    id              BIGSERIAL PRIMARY KEY,
    task_id         BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    level           TEXT NOT NULL DEFAULT 'info',
    message         TEXT NOT NULL
);
CREATE INDEX task_logs_task_idx ON task_logs (task_id, id);

CREATE TABLE approvals (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    task_id         BIGINT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    agent           TEXT NOT NULL,
    capability      TEXT NOT NULL,
    risk            TEXT NOT NULL,
    summary         TEXT NOT NULL,
    details         JSONB NOT NULL DEFAULT '{}'::jsonb,
    decided_by      TEXT NOT NULL DEFAULT '',
    decided_at      TIMESTAMPTZ,
    decision_note   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX approvals_status_idx ON approvals (status, created_at DESC);

CREATE TABLE events (
    id                  BIGSERIAL PRIMARY KEY,
    at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind                TEXT NOT NULL,
    severity            TEXT NOT NULL DEFAULT 'info' CHECK (severity IN ('info', 'warning', 'critical')),
    source              TEXT NOT NULL,
    title               TEXT NOT NULL,
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb,
    task_id             BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
    forward_status      TEXT NOT NULL DEFAULT 'pending'
                        CHECK (forward_status IN ('pending', 'skipped', 'sent', 'failed')),
    forward_attempts    INTEGER NOT NULL DEFAULT 0,
    forward_error       TEXT NOT NULL DEFAULT '',
    forward_next_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    forwarded_at        TIMESTAMPTZ,
    n8n_ack_at          TIMESTAMPTZ,
    n8n_execution_id    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX events_at_idx ON events (at DESC);
CREATE INDEX events_forward_idx ON events (id) WHERE forward_status = 'pending';

CREATE TABLE sites (
    id                  BIGSERIAL PRIMARY KEY,
    name                TEXT NOT NULL,
    url                 TEXT NOT NULL UNIQUE,
    client              TEXT NOT NULL DEFAULT '',
    notes               TEXT NOT NULL DEFAULT '',
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_status         TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (last_status IN ('unknown', 'up', 'degraded', 'down')),
    last_checked_at     TIMESTAMPTZ,
    last_check_id       BIGINT
);

CREATE TABLE site_checks (
    id                  BIGSERIAL PRIMARY KEY,
    site_id             BIGINT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    task_id             BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
    checked_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    status              TEXT NOT NULL CHECK (status IN ('up', 'degraded', 'down')),
    http_status         INTEGER,
    response_ms         INTEGER,
    final_url           TEXT NOT NULL DEFAULT '',
    tls_days_left       INTEGER,
    tls_issuer          TEXT NOT NULL DEFAULT '',
    is_wordpress        BOOLEAN NOT NULL DEFAULT FALSE,
    wp_version          TEXT NOT NULL DEFAULT '',
    findings            JSONB NOT NULL DEFAULT '[]'::jsonb,
    details             JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX site_checks_site_idx ON site_checks (site_id, checked_at DESC);

CREATE TABLE artifacts (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    task_id             BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
    agent               TEXT NOT NULL,
    kind                TEXT NOT NULL,
    title               TEXT NOT NULL,
    content             TEXT NOT NULL DEFAULT '',
    content_type        TEXT NOT NULL DEFAULT 'text/markdown',
    data                JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'approved', 'rejected', 'archived'))
);
CREATE INDEX artifacts_created_idx ON artifacts (created_at DESC);

CREATE TABLE worker_heartbeats (
    worker_id           TEXT PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    info                JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Per-integration health, written by whoever last talked to it.
CREATE TABLE integration_status (
    name                TEXT PRIMARY KEY,
    last_ok_at          TIMESTAMPTZ,
    last_error_at       TIMESTAMPTZ,
    last_error          TEXT NOT NULL DEFAULT '',
    detail              JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Calls made to a metered LLM, for the daily cap and the dashboard.
CREATE TABLE llm_usage (
    id                  BIGSERIAL PRIMARY KEY,
    at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    provider            TEXT NOT NULL,
    model               TEXT NOT NULL,
    purpose             TEXT NOT NULL,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    ok                  BOOLEAN NOT NULL DEFAULT TRUE,
    error               TEXT NOT NULL DEFAULT ''
);
CREATE INDEX llm_usage_at_idx ON llm_usage (at DESC);
