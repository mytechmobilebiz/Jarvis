-- 0.6.0: JARVIS Brain. Persistent memory for the agents, the archive of JARVIS's conversations, the record of
-- every import (for audit and rollback), conflicts waiting for a person, and JARVIS's scheduled jobs as
-- disabled workflow drafts.
--
-- Every statement is idempotent. Restoring a backup taken before 0.6.0 (scripts/restore.ps1) leaves these
-- tables (and their data) in place but removes this file's row from schema_migrations, so the next start runs it
-- again. No table here has a foreign key into a table that existed before 0.6.0: pg_restore --clean of an older
-- dump must be able to drop and recreate those tables (tests/integration/test_brain_schema.py).
--
-- Imported text is reference data. Nothing in these tables can approve, run or schedule anything: approvals
-- stay in `approvals`, and a workflow draft only becomes a schedule (disabled) through an admin's action.

CREATE TABLE IF NOT EXISTS import_runs (
    id                BIGSERIAL PRIMARY KEY,
    source_system     TEXT NOT NULL DEFAULT 'jarvis',
    label             TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'completed', 'failed', 'interrupted', 'rolled_back')),
    -- What was selected: sample size, session range, whether profile files were included.
    scope             JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Where it came from: file names, sizes, SHA-256 of the database backup and the profile manifest.
    source            JSONB NOT NULL DEFAULT '{}'::jsonb,
    importer_version  TEXT NOT NULL DEFAULT '',
    created_by        TEXT NOT NULL DEFAULT '',
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    -- Where an interrupted run continues: the phase and the last committed position.
    checkpoint        JSONB NOT NULL DEFAULT '{}'::jsonb,
    counts            JSONB NOT NULL DEFAULT '{}'::jsonb,
    report            JSONB NOT NULL DEFAULT '{}'::jsonb,
    error             TEXT NOT NULL DEFAULT '',
    rolled_back_at    TIMESTAMPTZ,
    rolled_back_by    TEXT NOT NULL DEFAULT ''
);
-- One import at a time.
CREATE UNIQUE INDEX IF NOT EXISTS import_runs_one_running_idx ON import_runs ((true)) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS import_runs_started_idx ON import_runs (started_at DESC);

-- One row per source record a run looked at, so every record is accounted for exactly once.
CREATE TABLE IF NOT EXISTS import_records (
    id            BIGSERIAL PRIMARY KEY,
    run_id        BIGINT NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
    source_key    TEXT NOT NULL,
    record_type   TEXT NOT NULL,
    action        TEXT NOT NULL CHECK (action IN ('imported', 'updated', 'duplicate', 'excluded', 'withheld', 'conflict')),
    reason        TEXT NOT NULL DEFAULT '',
    target_table  TEXT NOT NULL DEFAULT '',
    target_id     BIGINT,
    -- For 'updated': the target row's fields before this run changed them, so rollback can put them back.
    before        JSONB NOT NULL DEFAULT '{}'::jsonb,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, source_key)
);
CREATE INDEX IF NOT EXISTS import_records_source_key_idx ON import_records (source_key);
CREATE INDEX IF NOT EXISTS import_records_target_idx ON import_records (target_table, target_id);

-- Long-term memory: business facts, shared knowledge, agent notes, operating instructions (reference only),
-- skills and workflow references, project and task context, and decisions, accomplishments and problems.
-- agent = '' is shared; otherwise the one agent it belongs to. Restricted data (Credit Repair, SSNs, card and
-- bank details) is never stored here.
CREATE TABLE IF NOT EXISTS memory_items (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('business', 'knowledge', 'agent_note', 'instruction', 'skill',
                                                  'workflow', 'project', 'decision', 'accomplishment', 'problem')),
    agent           TEXT NOT NULL DEFAULT '' CHECK (agent ~ '^[a-z_]{0,40}$'),
    title           TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    body            TEXT NOT NULL DEFAULT '' CHECK (length(body) <= 200000),
    tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    sensitivity     TEXT NOT NULL DEFAULT 'general' CHECK (sensitivity IN ('general', 'customer')),
    -- quarantined: held a secret (now redacted) or other risky content; only an admin may review it.
    status          TEXT NOT NULL DEFAULT 'pending_review'
                    CHECK (status IN ('pending_review', 'quarantined', 'approved', 'rejected', 'archived')),
    origin          TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('import', 'manual', 'promoted')),
    -- Review hints: instruction-like text, approval conflicts, redactions, placeholders, template status.
    flags           JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_system   TEXT NOT NULL DEFAULT '',
    source_ref      TEXT NOT NULL DEFAULT '',
    source_key      TEXT UNIQUE,
    source_sha256   TEXT NOT NULL DEFAULT '',
    source_at       TIMESTAMPTZ,
    import_run_id   BIGINT REFERENCES import_runs(id) ON DELETE SET NULL,
    content_hash    TEXT NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT NOT NULL DEFAULT '',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      TEXT NOT NULL DEFAULT '',
    reviewed_by     TEXT NOT NULL DEFAULT '',
    reviewed_at     TIMESTAMPTZ,
    review_note     TEXT NOT NULL DEFAULT '',
    deleted_at      TIMESTAMPTZ,
    deleted_by      TEXT NOT NULL DEFAULT '',
    search          TSVECTOR GENERATED ALWAYS AS (
                        setweight(to_tsvector('english'::regconfig, title), 'A')
                        || setweight(to_tsvector('english'::regconfig, left(body, 100000)), 'B')) STORED
);
CREATE INDEX IF NOT EXISTS memory_items_search_idx ON memory_items USING GIN (search);
CREATE INDEX IF NOT EXISTS memory_items_kind_status_idx ON memory_items (kind, status);
CREATE INDEX IF NOT EXISTS memory_items_agent_idx ON memory_items (agent);
CREATE INDEX IF NOT EXISTS memory_items_content_hash_idx ON memory_items (content_hash);
CREATE INDEX IF NOT EXISTS memory_items_run_idx ON memory_items (import_run_id);
CREATE INDEX IF NOT EXISTS memory_items_updated_idx ON memory_items (updated_at DESC);

-- Every change to an item, in full, so any edit can be shown and undone.
CREATE TABLE IF NOT EXISTS memory_item_versions (
    id            BIGSERIAL PRIMARY KEY,
    item_id       BIGINT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    change        TEXT NOT NULL CHECK (change IN ('created', 'imported', 'edited', 'approved', 'rejected',
                                                  'archived', 'reopened', 'deleted', 'restored', 'redacted',
                                                  'reverted')),
    kind          TEXT NOT NULL,
    agent         TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    tags          JSONB NOT NULL DEFAULT '[]'::jsonb,
    sensitivity   TEXT NOT NULL,
    status        TEXT NOT NULL,
    changed_by    TEXT NOT NULL DEFAULT '',
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    note          TEXT NOT NULL DEFAULT '',
    UNIQUE (item_id, version)
);

-- JARVIS's conversations, read from the verified backup of its state.db. Secrets redacted, e-mail addresses and
-- phone numbers masked, reasoning never read; a message holding restricted data is kept without its text.
CREATE TABLE IF NOT EXISTS archive_sessions (
    id                        BIGSERIAL PRIMARY KEY,
    source_system             TEXT NOT NULL DEFAULT 'jarvis',
    source_key                TEXT NOT NULL UNIQUE,
    source_session_id         TEXT NOT NULL,
    title                     TEXT NOT NULL DEFAULT '',
    staff                     TEXT NOT NULL DEFAULT '',
    agent                     TEXT NOT NULL DEFAULT '',
    origin                    TEXT NOT NULL DEFAULT 'chat' CHECK (origin IN ('chat', 'scheduled')),
    channel                   TEXT NOT NULL DEFAULT '',
    lineage                   TEXT NOT NULL DEFAULT '' CHECK (lineage IN ('', 'continuation', 'branch', 'delegated')),
    parent_source_session_id  TEXT NOT NULL DEFAULT '',
    job_id                    TEXT NOT NULL DEFAULT '',
    job_name                  TEXT NOT NULL DEFAULT '',
    started_at                TIMESTAMPTZ,
    ended_at                  TIMESTAMPTZ,
    end_reason                TEXT NOT NULL DEFAULT '',
    hermes_archived           BOOLEAN NOT NULL DEFAULT FALSE,
    source_message_count      INTEGER NOT NULL DEFAULT 0,
    imported_messages         INTEGER NOT NULL DEFAULT 0,
    withheld_messages         INTEGER NOT NULL DEFAULT 0,
    tool_calls                INTEGER NOT NULL DEFAULT 0,
    sensitivity               TEXT NOT NULL DEFAULT 'general' CHECK (sensitivity IN ('general', 'customer', 'restricted')),
    labels                    JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary                   TEXT NOT NULL DEFAULT '',
    flags                     JSONB NOT NULL DEFAULT '{}'::jsonb,
    import_run_id             BIGINT REFERENCES import_runs(id) ON DELETE SET NULL,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    search                    TSVECTOR GENERATED ALWAYS AS (
                                  setweight(to_tsvector('english'::regconfig, title), 'A')
                                  || setweight(to_tsvector('english'::regconfig, left(summary, 20000)), 'B')) STORED
);
CREATE INDEX IF NOT EXISTS archive_sessions_search_idx ON archive_sessions USING GIN (search);
CREATE INDEX IF NOT EXISTS archive_sessions_started_idx ON archive_sessions (started_at DESC);
CREATE INDEX IF NOT EXISTS archive_sessions_agent_idx ON archive_sessions (agent, staff);
CREATE INDEX IF NOT EXISTS archive_sessions_run_idx ON archive_sessions (import_run_id);

CREATE TABLE IF NOT EXISTS archive_messages (
    id                 BIGSERIAL PRIMARY KEY,
    session_id         BIGINT NOT NULL REFERENCES archive_sessions(id) ON DELETE CASCADE,
    source_key         TEXT NOT NULL UNIQUE,
    source_message_id  TEXT NOT NULL,
    -- Hash of the source row (role, content, tool calls, active, compacted): a later import sees what changed.
    source_fp          TEXT NOT NULL DEFAULT '',
    position           INTEGER NOT NULL,
    role               TEXT NOT NULL,
    content            TEXT NOT NULL DEFAULT '',
    content_chars      INTEGER NOT NULL DEFAULT 0,
    truncated          BOOLEAN NOT NULL DEFAULT FALSE,
    tool_name          TEXT NOT NULL DEFAULT '',
    tool_call_id       TEXT NOT NULL DEFAULT '',
    tool_calls         JSONB NOT NULL DEFAULT '[]'::jsonb,
    at                 TIMESTAMPTZ,
    state              TEXT NOT NULL DEFAULT 'live' CHECK (state IN ('live', 'compacted')),
    sensitivity        TEXT NOT NULL DEFAULT 'general' CHECK (sensitivity IN ('general', 'customer', 'restricted')),
    withheld           BOOLEAN NOT NULL DEFAULT FALSE,
    redactions         JSONB NOT NULL DEFAULT '{}'::jsonb,
    labels             TEXT[] NOT NULL DEFAULT '{}',
    flags              JSONB NOT NULL DEFAULT '{}'::jsonb,
    import_run_id      BIGINT REFERENCES import_runs(id) ON DELETE SET NULL,
    CHECK (NOT withheld OR content = ''),
    search             TSVECTOR GENERATED ALWAYS AS (to_tsvector('english'::regconfig, left(content, 100000))) STORED
);
CREATE INDEX IF NOT EXISTS archive_messages_search_idx ON archive_messages USING GIN (search);
CREATE INDEX IF NOT EXISTS archive_messages_labels_idx ON archive_messages USING GIN (labels);
CREATE INDEX IF NOT EXISTS archive_messages_session_idx ON archive_messages (session_id, position);
CREATE INDEX IF NOT EXISTS archive_messages_at_idx ON archive_messages (at DESC);
CREATE INDEX IF NOT EXISTS archive_messages_run_idx ON archive_messages (import_run_id);

-- Something an import found that a person must decide: never resolved by overwriting.
CREATE TABLE IF NOT EXISTS memory_conflicts (
    id                BIGSERIAL PRIMARY KEY,
    run_id            BIGINT REFERENCES import_runs(id) ON DELETE CASCADE,
    kind              TEXT NOT NULL CHECK (kind IN ('memory_item', 'source_changed', 'instruction', 'schedule', 'site',
                                                    'workflow')),
    title             TEXT NOT NULL,
    source_key        TEXT NOT NULL DEFAULT '',
    -- The same question is asked once: kind, source and incoming content (decided conflicts included).
    dedupe_key        TEXT UNIQUE,
    existing_table    TEXT NOT NULL DEFAULT '',
    existing_id       BIGINT,
    incoming_item_id  BIGINT REFERENCES memory_items(id) ON DELETE CASCADE,
    existing          JSONB NOT NULL DEFAULT '{}'::jsonb,
    incoming          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status            TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'keep_existing', 'use_incoming', 'keep_both', 'dismissed')),
    -- What a resolution changed in existing rows (ids, versions and statuses before), so it can be reverted.
    effects           JSONB NOT NULL DEFAULT '{}'::jsonb,
    decided_by        TEXT NOT NULL DEFAULT '',
    decided_at        TIMESTAMPTZ,
    note              TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memory_conflicts_status_idx ON memory_conflicts (status, created_at DESC);

-- JARVIS's scheduled jobs and skills that could become Command Center workflows. A draft never runs: an admin
-- can turn a reviewed draft into a schedule, which is created disabled.
CREATE TABLE IF NOT EXISTS workflow_drafts (
    id                 BIGSERIAL PRIMARY KEY,
    source_system      TEXT NOT NULL DEFAULT 'jarvis',
    source_key         TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    staff              TEXT NOT NULL DEFAULT '',
    agent              TEXT NOT NULL DEFAULT '',
    capability         TEXT NOT NULL DEFAULT '',
    params             JSONB NOT NULL DEFAULT '{}'::jsonb,
    compatibility      TEXT NOT NULL CHECK (compatibility IN ('mapped', 'partial', 'reference_only')),
    trigger            TEXT NOT NULL DEFAULT 'schedule' CHECK (trigger IN ('schedule', 'manual', 'event')),
    cron               TEXT NOT NULL DEFAULT '',
    schedule_display   TEXT NOT NULL DEFAULT '',
    timezone           TEXT NOT NULL DEFAULT '',
    enabled_in_source  BOOLEAN NOT NULL DEFAULT FALSE,
    deliver            TEXT NOT NULL DEFAULT '',
    approval           JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_handling     JSONB NOT NULL DEFAULT '{}'::jsonb,
    duplicate_guard    JSONB NOT NULL DEFAULT '{}'::jsonb,
    spec               TEXT NOT NULL DEFAULT '',
    history            JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes              JSONB NOT NULL DEFAULT '[]'::jsonb,
    status             TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'reviewed', 'converted', 'rejected')),
    -- The disabled schedule an admin created from this draft. Not a foreign key (see the top of this file): the code
    -- checks that the schedule still exists.
    schedule_id        BIGINT,
    source_fp          TEXT NOT NULL DEFAULT '',
    import_run_id      BIGINT REFERENCES import_runs(id) ON DELETE SET NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by        TEXT NOT NULL DEFAULT '',
    reviewed_at        TIMESTAMPTZ
);

-- Source records an admin purged on purpose: a later import does not bring them back.
CREATE TABLE IF NOT EXISTS brain_tombstones (
    source_key   TEXT PRIMARY KEY,
    purged_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    purged_by    TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT ''
);
