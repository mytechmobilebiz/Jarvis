-- 0.7.0: the match-only index the Research Gateway checks outgoing web searches against, so a client's name, e-mail,
-- phone number or reference typed into a search is refused. Each row holds an HMAC-SHA256 under a key derived from
-- CREDIT_REPAIR_DATA_KEY (app/credit_repair/blind_index.py): nothing here can be read back or decrypted, and the
-- only answer that leaves this database is "match" or "no match".
--
-- Idempotent, and no foreign key into cr_clients, so a backup taken before 0.7.0 still restores with --clean. The
-- application fills it when a client is created or changed, and rebuilds it for every client at every start (rows a
-- restore left behind never outlive a restart).

CREATE TABLE IF NOT EXISTS cr_research_index (
    client_id   BIGINT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('name_pair', 'name_joined', 'full_name', 'email', 'phone', 'reference')),
    mac         TEXT NOT NULL CHECK (mac ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (client_id, kind, mac)
);
CREATE INDEX IF NOT EXISTS cr_research_index_mac_idx ON cr_research_index (mac);

-- How the rows were made: their version and an HMAC of a fixed text under the same key. While either differs from
-- the running code's (CREDIT_REPAIR_DATA_KEY changed, or rows of another version), every search is refused.
CREATE TABLE IF NOT EXISTS cr_research_index_meta (
    id          SMALLINT PRIMARY KEY CHECK (id = 1),
    key_check   TEXT NOT NULL CHECK (key_check ~ '^[0-9a-f]{64}$'),
    version     INTEGER NOT NULL,
    built_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
