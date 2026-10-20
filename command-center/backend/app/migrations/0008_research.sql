-- 0.7.0: the Research Gateway (app/research). Searches the public web through the private SearXNG container for a
-- few agents, with every query screened first, and keeps a log of every attempt, the results and the pages they cite,
-- a short-lived cache, usage counters and the per-agent policy.
--
-- New tables only; no existing table changes. Every statement is idempotent. No table here has a foreign key into a
-- table that existed before 0.7.0 (task, request and memory item ids are plain BIGINT columns the code checks), so
-- pg_restore --clean of an older backup still drops and recreates those tables, and going back to 0.6.x leaves these
-- tables unused. Research is off until an admin switches it on (app_settings key research_mode, absent = off).
--
-- Web text is untrusted reference data: nothing here can approve, run or schedule anything.

-- One row per attempt, refused ones included. A blocked attempt keeps a reason code only (plus the lead or ticket
-- that caused it): never the text, never a hash of it (a hash of a phone number or a name can be reversed by trying
-- values). The CHECK below holds that for every row the screen or the policy refused.
CREATE TABLE IF NOT EXISTS research_queries (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    task_id           BIGINT,
    request_id        BIGINT,
    agent             TEXT NOT NULL,
    capability        TEXT NOT NULL DEFAULT '',
    requested_by      TEXT NOT NULL DEFAULT '',
    -- Where the work came from, read from the database (app/research/gateway.py). Only the first four may search;
    -- the others are recorded when they are refused.
    trigger_source    TEXT NOT NULL CHECK (trigger_source IN ('dashboard', 'supervisor', 'retry', 'test',
                                                              'schedule', 'n8n', 'service', 'unknown')),
    kind              TEXT NOT NULL DEFAULT 'search' CHECK (kind IN ('search', 'fetch')),
    -- The canonical, screened text: exactly what was sent.
    query_text        TEXT CHECK (length(query_text) BETWEEN 1 AND 128),
    target_url        TEXT CHECK (length(target_url) <= 2048),
    blocked_by_ref    TEXT CHECK (blocked_by_ref ~ '^(lead|ticket):[0-9]+$'),
    options           JSONB NOT NULL DEFAULT '{}'::jsonb,
    provider          TEXT NOT NULL DEFAULT '',
    provider_version  TEXT NOT NULL DEFAULT '',
    mode              TEXT NOT NULL DEFAULT 'off' CHECK (mode IN ('off', 'mock', 'live')),
    status            TEXT NOT NULL,
    reason_code       TEXT NOT NULL DEFAULT '',
    http_status       INTEGER,
    result_count      INTEGER NOT NULL DEFAULT 0,
    cached            BOOLEAN NOT NULL DEFAULT FALSE,
    -- The engines asked, and those that did not answer ([engine, reason] pairs, codes only).
    engines           JSONB NOT NULL DEFAULT '[]'::jsonb,
    unresponsive      JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Hints about the query itself (instruction_like); a hint never refuses.
    flags             JSONB NOT NULL DEFAULT '[]'::jsonb,
    cache_key         TEXT,
    -- Money columns for a paid provider; SearXNG costs nothing, so they stay 'no' and empty in this release.
    billable          TEXT NOT NULL DEFAULT 'no' CHECK (billable IN ('no', 'yes', 'possibly')),
    unit_price_micro  BIGINT,
    currency          TEXT NOT NULL DEFAULT '',
    cost_micro        BIGINT NOT NULL DEFAULT 0,
    latency_ms        INTEGER,
    fallback_of       BIGINT,
    CHECK (status NOT IN ('blocked', 'refused_policy')
           OR (query_text IS NULL AND target_url IS NULL AND cache_key IS NULL AND options = '{}'::jsonb)),
    CHECK (blocked_by_ref IS NULL OR status = 'blocked')
);
CREATE INDEX IF NOT EXISTS research_queries_created_idx ON research_queries (created_at DESC);
CREATE INDEX IF NOT EXISTS research_queries_agent_idx ON research_queries (agent, created_at DESC);
CREATE INDEX IF NOT EXISTS research_queries_status_idx ON research_queries (status, created_at DESC);
CREATE INDEX IF NOT EXISTS research_queries_task_idx ON research_queries (task_id);

-- Each page seen, once, keyed by the SHA-256 of its normalized URL, so many queries can cite it. Kept 30 days
-- (expires_at) unless promoted.
CREATE TABLE IF NOT EXISTS research_sources (
    id                  BIGSERIAL PRIMARY KEY,
    url                 TEXT NOT NULL CHECK (length(url) <= 2048),
    url_sha256          TEXT NOT NULL UNIQUE,
    domain              TEXT NOT NULL DEFAULT '',
    title               TEXT NOT NULL DEFAULT '' CHECK (length(title) <= 300),
    site_name           TEXT NOT NULL DEFAULT '',
    published_at        TIMESTAMPTZ,
    first_retrieved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_retrieved_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    provider            TEXT NOT NULL DEFAULT '',
    -- For a fetched page (not in this release): its cleaned text, at most 100 KB.
    fetched_text        TEXT CHECK (length(fetched_text) <= 102400),
    content_sha256      TEXT NOT NULL DEFAULT '',
    truncated           BOOLEAN NOT NULL DEFAULT FALSE,
    flags               JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'withheld', 'purged')),
    expires_at          TIMESTAMPTZ,
    -- A memory item made from this page (not in this release). Not a foreign key: see the top of this file.
    promoted_item_id    BIGINT
);
CREATE INDEX IF NOT EXISTS research_sources_expires_idx ON research_sources (expires_at);
CREATE INDEX IF NOT EXISTS research_sources_domain_idx ON research_sources (domain);

-- A query's results in their order. Snippets belong to the query (the same page can show different text).
CREATE TABLE IF NOT EXISTS research_results (
    id              BIGSERIAL PRIMARY KEY,
    query_id        BIGINT NOT NULL REFERENCES research_queries(id) ON DELETE CASCADE,
    rank            INTEGER NOT NULL CHECK (rank >= 1),
    source_id       BIGINT REFERENCES research_sources(id) ON DELETE SET NULL,
    snippet         TEXT NOT NULL DEFAULT '' CHECK (length(snippet) <= 1200),
    provider_score  DOUBLE PRECISION,
    authority       DOUBLE PRECISION,
    engines         JSONB NOT NULL DEFAULT '[]'::jsonb,
    type            TEXT NOT NULL DEFAULT 'web' CHECK (type IN ('web', 'reference')),
    flags           JSONB NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (query_id, rank)
);
CREATE INDEX IF NOT EXISTS research_results_source_idx ON research_results (source_id);

-- Answers already cleaned, shared by everyone who asks the same question. Never records who asked.
CREATE TABLE IF NOT EXISTS research_cache (
    cache_key   TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('search', 'fetch')),
    provider    TEXT NOT NULL,
    response    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS research_cache_expires_idx ON research_cache (expires_at);

-- Daily totals for the usage screens (UTC days).
CREATE TABLE IF NOT EXISTS research_usage_daily (
    day                DATE NOT NULL,
    provider           TEXT NOT NULL,
    agent              TEXT NOT NULL,
    kind               TEXT NOT NULL CHECK (kind IN ('search', 'fetch')),
    reserved           INTEGER NOT NULL DEFAULT 0,
    sent               INTEGER NOT NULL DEFAULT 0,
    billable           INTEGER NOT NULL DEFAULT 0,
    possibly_billable  INTEGER NOT NULL DEFAULT 0,
    cache_hits         INTEGER NOT NULL DEFAULT 0,
    blocked            INTEGER NOT NULL DEFAULT 0,
    failed             INTEGER NOT NULL DEFAULT 0,
    cost_micro         BIGINT NOT NULL DEFAULT 0,
    currency           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (day, provider, agent, kind)
);

-- One budget slot per search about to be sent, taken before the call in its own transaction (app/research/budget.py).
-- The daily caps count 'sent' slots and 'reserved' ones younger than ten minutes: a worker that crashed mid-call
-- frees its slot after that.
CREATE TABLE IF NOT EXISTS research_reservations (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    day          DATE NOT NULL,
    provider     TEXT NOT NULL,
    agent        TEXT NOT NULL,
    person       TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'search' CHECK (kind IN ('search', 'fetch')),
    status       TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved', 'sent', 'released', 'expired')),
    query_id     BIGINT,
    finished_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS research_reservations_day_idx ON research_reservations (day, provider, kind, status);

-- Spacing between searches per provider (a token bucket: one token every few seconds, a small burst).
CREATE TABLE IF NOT EXISTS research_rate (
    provider      TEXT PRIMARY KEY,
    last_sent_at  TIMESTAMPTZ,
    burst_tokens  DOUBLE PRECISION NOT NULL DEFAULT 0
);

-- Which agents may search, how often, and with which data classes (G general, S software names and versions typed
-- by a person, O the owner's own names, C client names). An agent without a row may not search. Credit Repair never
-- searches, whatever its row says: the code refuses it (app/research/policy.py).
CREATE TABLE IF NOT EXISTS research_policy (
    agent             TEXT PRIMARY KEY CHECK (agent ~ '^[a-z_]{1,40}$'),
    search_enabled    BOOLEAN NOT NULL DEFAULT FALSE,
    search_daily_cap  INTEGER NOT NULL DEFAULT 0 CHECK (search_daily_cap BETWEEN 0 AND 10000),
    data_classes      JSONB NOT NULL DEFAULT '[]'::jsonb
                      CHECK (jsonb_typeof(data_classes) = 'array' AND data_classes <@ '["G", "S", "O", "C"]'::jsonb),
    blocked_domains   JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(blocked_domains) = 'array'),
    updated_by        TEXT NOT NULL DEFAULT '',
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The proposed defaults (plan section 10; owner decision 4 open); an existing row is never overwritten.
INSERT INTO research_policy (agent, search_enabled, search_daily_cap, data_classes, updated_by) VALUES
    ('supervisor', TRUE, 20, '["G", "S", "O"]', 'default'),
    ('seo_content', TRUE, 20, '["G", "O"]', 'default'),
    ('social_marketing', TRUE, 10, '["G", "O"]', 'default'),
    ('wordpress_security', TRUE, 10, '["S"]', 'default'),
    ('server_management', TRUE, 5, '["S"]', 'default'),
    ('monitoring', FALSE, 0, '[]', 'default'),
    ('communications', FALSE, 0, '[]', 'default'),
    ('credit_repair', FALSE, 0, '[]', 'default')
ON CONFLICT (agent) DO NOTHING;
