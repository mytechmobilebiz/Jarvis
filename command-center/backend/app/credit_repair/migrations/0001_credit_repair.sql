-- The Credit Repair store. Lives in its own database ("credit_repair"), owned
-- by its own login role; the main application role has no access to it.
-- Every personal field (*_enc) is encrypted by the application with
-- CREDIT_REPAIR_DATA_KEY before it reaches the database.

CREATE TABLE cr_clients (
    id                      BIGSERIAL PRIMARY KEY,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by              TEXT NOT NULL,
    -- A non-identifying reference (CR-0001) that may appear outside this database.
    reference               TEXT NOT NULL UNIQUE,
    full_name_enc           TEXT NOT NULL,
    address_enc             TEXT NOT NULL DEFAULT '',
    email_enc               TEXT NOT NULL DEFAULT '',
    phone_enc               TEXT NOT NULL DEFAULT '',
    dob_enc                 TEXT NOT NULL DEFAULT '',
    -- Data minimisation: only the last four digits of an SSN are ever kept.
    ssn_last4_enc           TEXT NOT NULL DEFAULT '',
    notes_enc               TEXT NOT NULL DEFAULT '',
    status                  TEXT NOT NULL DEFAULT 'intake' CHECK (status IN ('intake', 'active', 'paused', 'closed')),
    contract_signed_on      DATE,
    services_completed_on   DATE,
    fees_collected_cents    INTEGER NOT NULL DEFAULT 0 CHECK (fees_collected_cents >= 0)
);

CREATE TABLE cr_items (
    id                      BIGSERIAL PRIMARY KEY,
    client_id               BIGINT NOT NULL REFERENCES cr_clients(id) ON DELETE CASCADE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    bureau                  TEXT NOT NULL CHECK (bureau IN ('equifax', 'experian', 'transunion')),
    creditor_enc            TEXT NOT NULL,
    account_last4_enc       TEXT NOT NULL DEFAULT '',
    reason                  TEXT NOT NULL CHECK (reason IN ('not_mine', 'inaccurate_balance', 'inaccurate_status',
                                                            'duplicate', 'outdated', 'other')),
    detail_enc              TEXT NOT NULL DEFAULT '',
    status                  TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_letter', 'disputed', 'verified', 'corrected', 'deleted'))
);
CREATE INDEX cr_items_client_idx ON cr_items (client_id);

CREATE TABLE cr_letters (
    id                      BIGSERIAL PRIMARY KEY,
    client_id               BIGINT NOT NULL REFERENCES cr_clients(id) ON DELETE CASCADE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    bureau                  TEXT NOT NULL,
    item_ids                BIGINT[] NOT NULL,
    content_enc             TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'mailed_mock', 'void')),
    task_id                 BIGINT,
    mailed_at               TIMESTAMPTZ
);
CREATE INDEX cr_letters_client_idx ON cr_letters (client_id);

-- Every read or write of client data, by whom.
CREATE TABLE cr_access_log (
    id                      BIGSERIAL PRIMARY KEY,
    at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor                   TEXT NOT NULL,
    action                  TEXT NOT NULL,
    client_id               BIGINT,
    detail                  JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX cr_access_log_at_idx ON cr_access_log (at DESC);
