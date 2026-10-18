-- Records for the specialist agents: leads (Sales), support tickets (Support),
-- and the outbox, where every outward action approved through a *mock*
-- integration is recorded instead of being sent.

-- Access to the isolated Credit Repair data (a separate database) is a
-- per-user permission, off for everyone by default, admins included.
ALTER TABLE users ADD COLUMN can_access_sensitive BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE leads (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source              TEXT NOT NULL DEFAULT 'dashboard',
    name                TEXT NOT NULL,
    company             TEXT NOT NULL DEFAULT '',
    email               TEXT NOT NULL DEFAULT '',
    phone               TEXT NOT NULL DEFAULT '',
    website             TEXT NOT NULL DEFAULT '',
    service_interest    TEXT NOT NULL DEFAULT '',
    message             TEXT NOT NULL DEFAULT '',
    score               INTEGER,
    score_reasons       JSONB NOT NULL DEFAULT '[]'::jsonb,
    status              TEXT NOT NULL DEFAULT 'new'
                        CHECK (status IN ('new', 'qualified', 'proposal_drafted', 'proposal_sent', 'won', 'lost', 'spam')),
    proposal_artifact_id BIGINT REFERENCES artifacts(id) ON DELETE SET NULL
);
CREATE INDEX leads_created_idx ON leads (created_at DESC);

CREATE TABLE support_tickets (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source              TEXT NOT NULL DEFAULT 'dashboard',
    customer_name       TEXT NOT NULL DEFAULT '',
    customer_email      TEXT NOT NULL DEFAULT '',
    subject             TEXT NOT NULL,
    body                TEXT NOT NULL DEFAULT '',
    category            TEXT NOT NULL DEFAULT '',
    priority            TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
    sentiment           TEXT NOT NULL DEFAULT '',
    triage              JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'triaged', 'drafted', 'replied', 'closed')),
    reply_artifact_id   BIGINT REFERENCES artifacts(id) ON DELETE SET NULL
);
CREATE INDEX support_tickets_created_idx ON support_tickets (created_at DESC);

CREATE TABLE outbox (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    task_id             BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
    agent               TEXT NOT NULL,
    integration         TEXT NOT NULL,
    mode                TEXT NOT NULL DEFAULT 'mock' CHECK (mode IN ('mock', 'live')),
    action              TEXT NOT NULL,
    recipient           TEXT NOT NULL DEFAULT '',
    subject             TEXT NOT NULL DEFAULT '',
    body                TEXT NOT NULL DEFAULT '',
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    approved_by         TEXT NOT NULL,
    result              TEXT NOT NULL DEFAULT 'recorded, not sent'
);
CREATE INDEX outbox_created_idx ON outbox (created_at DESC);
