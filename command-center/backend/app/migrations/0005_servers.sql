-- 0.3.0: servers reached over SSH through the restricted cc-gate, and the
-- audit trail of every command sent to them.

CREATE TABLE servers (
    id                    BIGSERIAL PRIMARY KEY,
    name                  TEXT NOT NULL UNIQUE,
    host                  TEXT NOT NULL,
    port                  INTEGER NOT NULL DEFAULT 22 CHECK (port BETWEEN 1 AND 65535),
    username              TEXT NOT NULL CHECK (username <> 'root'),
    environment           TEXT NOT NULL DEFAULT 'production' CHECK (environment IN ('production', 'staging', 'test')),
    -- The host key the server must present, pinned after a person compared its
    -- fingerprint. Empty until then: no command runs on an unpinned server.
    host_key              TEXT NOT NULL DEFAULT '',
    host_key_fingerprint  TEXT NOT NULL DEFAULT '',
    host_key_trusted_by   TEXT NOT NULL DEFAULT '',
    host_key_trusted_at   TIMESTAMPTZ,
    -- What the server offered at the last scan; not trusted.
    offered_host_key      TEXT NOT NULL DEFAULT '',
    offered_fingerprint   TEXT NOT NULL DEFAULT '',
    enabled               BOOLEAN NOT NULL DEFAULT TRUE,
    notes                 TEXT NOT NULL DEFAULT '',
    last_hello            JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen_at          TIMESTAMPTZ,
    created_by            TEXT NOT NULL DEFAULT '',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE server_commands (
    id              BIGSERIAL PRIMARY KEY,
    server_id       BIGINT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    task_id         BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
    op              TEXT NOT NULL,
    args            JSONB NOT NULL DEFAULT '{}'::jsonb,
    kind            TEXT NOT NULL CHECK (kind IN ('read', 'change')),
    requested_by    TEXT NOT NULL DEFAULT '',
    approved_by     TEXT NOT NULL DEFAULT '',
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    duration_ms     INTEGER,
    exit_status     INTEGER,
    ok              BOOLEAN,
    refused         BOOLEAN NOT NULL DEFAULT FALSE,
    result          JSONB,
    error           TEXT NOT NULL DEFAULT '',
    output_sha256   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX server_commands_server_idx ON server_commands (server_id, started_at DESC);
CREATE INDEX server_commands_task_idx ON server_commands (task_id);

ALTER TABLE sites ADD COLUMN environment TEXT NOT NULL DEFAULT 'production'
    CHECK (environment IN ('production', 'staging', 'test'));
ALTER TABLE sites ADD COLUMN server_id BIGINT REFERENCES servers(id) ON DELETE SET NULL;
ALTER TABLE sites ADD COLUMN server_site_key TEXT NOT NULL DEFAULT '';
-- The latest server-side (WP-CLI) diagnosis, for reports and change detection.
ALTER TABLE sites ADD COLUMN last_diagnosis JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE sites ADD COLUMN last_diagnosis_at TIMESTAMPTZ;

-- The nightly security scan now belongs to the WordPress Security & Repair agent.
UPDATE schedules
   SET agent = 'wordpress_security', capability = 'security_scan_all', params = '{}'::jsonb,
       description = 'Nightly security scan of every site: exposures, suspicious redirects and cloaking, injected content.',
       updated_at = now()
 WHERE name = 'wordpress-security-scan' AND agent = 'monitoring' AND capability = 'check_all_sites';
