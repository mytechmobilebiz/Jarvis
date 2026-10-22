-- 0.6.2: durable withholding (docs/JARVIS_BRAIN_SPEC.md 2.9; the 0.6.2 plan, "Decide once, replay exactly, hold what
-- is new": scratchpad conv/RECOMMENDATION.md sections 3 and 4).
--
-- An admin's withhold is bound to the identity of the JARVIS message, not only to the importer's derived source_key.
-- Every part of that identity is one more 'withheld by an admin' row in brain_tombstones (a "stone"): its key, plus a
-- binding (JARVIS session id, JARVIS row id, role, tool_call_id, the row's time, message_uid), the admin's own stone it
-- belongs to (anchor_key), how it joined (via), whether an admin still has to look at it (review), who kept it, and
-- the JARVIS backup the decision was taken on (decided_on: that backup's SHA-256). An import of that very backup
-- replays the decisions word for word (importer.Processor: replay); every other backup is a later one.
--
-- Exact identity (identity.exact, brain_exact() below): the same JARVIS session and row id, the same role and
-- tool_call_id, the same time (when both sides have one), and never two different uids. Every 'same JARVIS row' rule
-- uses it: in Python, in the repair below and in the database guard. Hermes reuses row ids after a state.db snapshot
-- comes back (sqlite_sequence with it): a new message at a reused row id has another time and another uid, so it is
-- never the withheld one (review 3, rv3-ov-0).
--
-- Idempotent, and safe wherever 0006_brain and 0007_brain_withhold ran: a fresh database and 0.5.0 (0006, 0007, 0008
-- run in one transaction), 0.6.0, 0.6.1, and restore.ps1 of an older dump (the restored tables come back without
-- these columns and without this file's schema_migrations row, so it runs again). No foreign key into a table older
-- than 0.6.0 (0006_brain.sql, top).
--
-- Brain data an importer older than 1.1.0 wrote (0.6.0, 0.6.1), or withholds made before this file, is legacy: it is
-- marked (brain_maintenance 'legacy_brain_data'), never re-derived. The rules that re-derived it (the upgrade's
-- binding of unbound stones, carried-on links paired by text alone, 0.6.1's plain fingerprints, the update-record
-- heuristics) are gone; the legacy reset (brain-import legacy-reset) re-imports such data from the JARVIS backup.
--
-- What brain_withhold_repair() still does (the start-up check, every import, rescan --yes and brain-import repair
-- call it; idempotent):
--   R4 readable rows that are a withheld message: by its key (a), by its exact JARVIS row (b), by a Hermes fold that
--      absorbed its uid (e);
--   R5 readable rows left in a restricted conversation are withheld (finding 3);
--   R6 withheld rows and their rollback copies keep no text fingerprint: source_fp becomes 'w1:<state>';
--   their rollback copies are blanked, their sessions marked for recompute, one audit entry with counts.
--   R7 every bound stone keeps the time of the JARVIS row it names (a time, never text: Hermes keeps it in each copy).
-- The start-up step (app/brain/identity.repair) then empties promoted copies, scrubs quotes and recomputes the
-- marked sessions. A BEFORE INSERT OR UPDATE trigger keeps every row a stone names without text, whatever code
-- writes it.

-- 0. Legacy Brain data, and an older importer still running (plan 1.7) ------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_maintenance (
    name     TEXT PRIMARY KEY,
    done_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail   JSONB NOT NULL DEFAULT '{}'::jsonb
);
-- Whether an import run's importer_version ('1.0.1') is older than 1.1.0 (none or unparsable: older).
CREATE OR REPLACE FUNCTION brain_importer_older(p_version text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $fn$
    SELECT coalesce(string_to_array(substring(coalesce(p_version, '') FROM '^\d+\.\d+\.\d+'), '.')::int[]
                    < ARRAY[1, 1, 0], true)
$fn$;
DO $$
DECLARE
    v_running bigint;
    v_runs int;
    v_withholds int := 0;
BEGIN
    -- An import by an older importer that is running now (it holds its run's advisory lock, (7314002213, run id) folded
    -- into 32 bits as importer.LOCK_KEY): this schema is not installed under it. Its run row predates this file, so
    -- the refusal of section 7 would never see it, and it would finish on this schema (review 3, rv3-dur-2).
    SELECT r.id INTO v_running FROM import_runs r
     WHERE r.status = 'running' AND brain_importer_older(r.importer_version)
       AND EXISTS (SELECT 1 FROM pg_locks l WHERE l.locktype = 'advisory' AND l.granted
                      AND l.classid = 3019034917::oid AND l.objid = r.id::oid AND l.objsubid = 2)
     ORDER BY r.id LIMIT 1;
    IF v_running IS NOT NULL THEN
        RAISE EXCEPTION 'an import by an importer older than 1.1.0 (run %) is still running: let it finish (or stop '
                        'it), then start Command Center 0.6.2 again', v_running;
    END IF;
    -- Legacy: a run by an importer older than 1.1.0 that was not rolled back, or a withhold made before this file (its
    -- stones have no decision backup: the column below is new).
    SELECT count(*) INTO v_runs FROM import_runs r
     WHERE r.status <> 'rolled_back' AND brain_importer_older(r.importer_version);
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
                      AND table_name = 'brain_tombstones' AND column_name = 'decided_on') THEN
        SELECT count(*) INTO v_withholds FROM brain_tombstones WHERE reason = 'withheld by an admin';
    END IF;
    IF v_runs > 0 OR v_withholds > 0 THEN
        INSERT INTO brain_maintenance (name, detail)
        VALUES ('legacy_brain_data', jsonb_build_object('runs_by_an_older_importer', v_runs,
                                                        'withholds_before_0008', v_withholds,
                                                        'found_by', '0008_brain_withhold_identity'))
        ON CONFLICT (name) DO NOTHING;
    END IF;
END
$$;

-- 1. Stones: the identity of a withheld message ------------------------------------------------------------------
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS source_session_id TEXT    NOT NULL DEFAULT '';
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS source_row_id     TEXT    NOT NULL DEFAULT '';
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS role              TEXT    NOT NULL DEFAULT '';
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS tool_call_id      TEXT    NOT NULL DEFAULT '';
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS message_uid       TEXT    NOT NULL DEFAULT '';
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS uid_known         BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS anchor_key        TEXT    NOT NULL DEFAULT '';
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS via               TEXT    NOT NULL DEFAULT '';
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS review            TEXT    NOT NULL DEFAULT '';
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS bound             BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS after_run         BIGINT  NOT NULL DEFAULT 0;
-- the admin hid the conversation's title with this withhold (an admin's own stone): an import that creates the
-- conversation again (after a rollback or a restore), and its continuations and branches, keep it hidden
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS title_hidden      BOOLEAN NOT NULL DEFAULT FALSE;
-- the time of the JARVIS row the stone is bound to (a time, never text): part of its exact identity, and Hermes keeps a
-- row's timestamp in every copy it makes
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS source_at         TIMESTAMPTZ;
-- the JARVIS backup (its SHA-256) the decision was taken on: an import of that very backup replays it word for word
-- ('' unknown: a decision of a version 4 file or older, never replayed)
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS decided_on        TEXT    NOT NULL DEFAULT '';
-- who kept a hold, and when (an admin's keep is a decision of its own; review 3, rv3-dur-6)
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS kept_by           TEXT    NOT NULL DEFAULT '';
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS kept_at           TIMESTAMPTZ;
-- a hold's computed evidence (0.6.2 plan 4.6): relation codes (identical, contains, part_of, restated, same_uid,
-- same_call, same_time, reused_row, stale, orphan, not_in_backup, none), never text and never a fingerprint of it
ALTER TABLE brain_tombstones ADD COLUMN IF NOT EXISTS relations         TEXT[]  NOT NULL DEFAULT '{}';
-- an import never learns a stone's uid any more (review 2 S9's learning time goes with it; rv3-dur-4 is moot)
ALTER TABLE brain_tombstones DROP COLUMN IF EXISTS uid_learned_at;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'brain_tombstones'::regclass
                                                AND conname = 'brain_tombstones_review_check') THEN
        ALTER TABLE brain_tombstones ADD CONSTRAINT brain_tombstones_review_check
            CHECK (review IN ('', 'pending', 'kept'));
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS brain_tombstones_row_idx ON brain_tombstones (source_row_id, source_session_id)
    WHERE source_row_id <> '';
CREATE INDEX IF NOT EXISTS brain_tombstones_uid_idx ON brain_tombstones (message_uid) WHERE message_uid <> '';
CREATE INDEX IF NOT EXISTS brain_tombstones_anchor_idx ON brain_tombstones (anchor_key) WHERE anchor_key <> '';
CREATE INDEX IF NOT EXISTS brain_tombstones_session_idx ON brain_tombstones (source_session_id)
    WHERE source_session_id <> '';

-- An admin's release of a held row, or identical text an admin left readable: bound to the exact JARVIS row it names
-- (session, row id, role, tool_call_id, time, uid) and to the backup it was decided on. It covers that row only, never
-- another row at a reused row id; stored family rows never change their text, so the row is the content released.
CREATE TABLE IF NOT EXISTS brain_withhold_exemptions (
    id                 BIGSERIAL PRIMARY KEY,
    anchor_key         TEXT NOT NULL,
    source_session_id  TEXT NOT NULL,
    source_row_id      TEXT NOT NULL,
    role               TEXT NOT NULL DEFAULT '',
    tool_call_id       TEXT NOT NULL DEFAULT '',
    source_at          TIMESTAMPTZ,
    source_uid         TEXT NOT NULL DEFAULT '',
    kind               TEXT NOT NULL DEFAULT 'release',
    decided_on         TEXT NOT NULL DEFAULT '',
    reason             TEXT NOT NULL DEFAULT 'released by an admin',
    decided_by         TEXT NOT NULL DEFAULT '',
    decided_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A database where an earlier build of this file ran (development only): the same table, bound by row id.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
                      AND table_name = 'brain_withhold_exemptions' AND column_name = 'id') THEN
        ALTER TABLE brain_withhold_exemptions DROP CONSTRAINT IF EXISTS brain_withhold_exemptions_pkey;
        ALTER TABLE brain_withhold_exemptions ADD COLUMN id BIGSERIAL PRIMARY KEY;
    END IF;
END
$$;
ALTER TABLE brain_withhold_exemptions ADD COLUMN IF NOT EXISTS role         TEXT NOT NULL DEFAULT '';
ALTER TABLE brain_withhold_exemptions ADD COLUMN IF NOT EXISTS tool_call_id TEXT NOT NULL DEFAULT '';
ALTER TABLE brain_withhold_exemptions ADD COLUMN IF NOT EXISTS source_at    TIMESTAMPTZ;
ALTER TABLE brain_withhold_exemptions ADD COLUMN IF NOT EXISTS source_uid   TEXT NOT NULL DEFAULT '';
ALTER TABLE brain_withhold_exemptions ADD COLUMN IF NOT EXISTS kind         TEXT NOT NULL DEFAULT 'release';
ALTER TABLE brain_withhold_exemptions ADD COLUMN IF NOT EXISTS decided_on   TEXT NOT NULL DEFAULT '';
-- no digest and no length of any text: the exact binding replaces them
ALTER TABLE brain_withhold_exemptions DROP COLUMN IF EXISTS content_chars;
ALTER TABLE brain_withhold_exemptions DROP COLUMN IF EXISTS content_fp;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'brain_withhold_exemptions'::regclass
                                                AND conname = 'brain_withhold_exemptions_kind_check') THEN
        ALTER TABLE brain_withhold_exemptions ADD CONSTRAINT brain_withhold_exemptions_kind_check
            CHECK (kind IN ('release', 'left_readable', 'quarantine_release'));
    END IF;
END
$$;
-- One decision per exact row, group and decision backup (a role or tool_call_id '' and a time NULL: not known, a
-- version 4 file's release). A release covers the row's content as it was in the backup it was decided on: an import
-- of another backup holds that row again unless its stored row already shows that very content (importer).
DROP INDEX IF EXISTS brain_withhold_exemptions_exact_idx;
CREATE UNIQUE INDEX brain_withhold_exemptions_exact_idx ON brain_withhold_exemptions
    (anchor_key, source_session_id, source_row_id, role, tool_call_id, source_uid,
     coalesce(source_at, '-infinity'::timestamptz), decided_on);
CREATE INDEX IF NOT EXISTS brain_withhold_exemptions_row_idx ON brain_withhold_exemptions
    (source_session_id, source_row_id);

-- 2. Archive: the JARVIS uid of a stored row, and the indexes the identity rules use --------------------------------
ALTER TABLE archive_messages ADD COLUMN IF NOT EXISTS source_uid TEXT NOT NULL DEFAULT '';
UPDATE archive_messages SET source_uid = substring(source_key FROM 20)
 WHERE source_uid = '' AND source_key LIKE 'jarvis:message:uid:%';
CREATE INDEX IF NOT EXISTS archive_messages_source_row_idx ON archive_messages (session_id, source_message_id);
CREATE INDEX IF NOT EXISTS archive_messages_source_uid_idx ON archive_messages (source_uid) WHERE source_uid <> '';
-- The uids a Hermes fold merged into this row (absorbed_message_uids: a compaction carrier, a micro-compaction
-- merge): the row holds their text, so a withhold of one of them withholds it too (group_rows, repair R4e).
ALTER TABLE archive_messages ADD COLUMN IF NOT EXISTS absorbed_uids TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS archive_messages_absorbed_uids_idx ON archive_messages USING gin (absorbed_uids);
CREATE INDEX IF NOT EXISTS archive_sessions_parent_idx ON archive_sessions (parent_source_session_id)
    WHERE parent_source_session_id <> '';
-- The rollback copies of rows an import deleted (a rewound original, a branch child's rewound copy): a withhold finds
-- those of the same message by uid, or by the conversation for its rewound twin (identity.group_rows, review 1 R29).
CREATE INDEX IF NOT EXISTS import_records_deleted_uid_idx ON import_records ((before -> 'deleted_row' ->> 'source_uid'))
    WHERE record_type = 'message' AND before ? 'deleted_row';
CREATE INDEX IF NOT EXISTS import_records_deleted_session_idx
    ON import_records ((before -> 'deleted_row' ->> 'session_id'))
    WHERE record_type = 'message' AND before ? 'deleted_row';

-- A gateway reset (Hermes _reset_from: a new session that copies no rows) is recorded as its own lineage, never a family
-- link (app/brain/source.py; 0.6.2 plan 2.1).
DO $$
DECLARE
    c text;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'archive_sessions'::regclass AND contype = 'c'
                AND pg_get_constraintdef(oid) LIKE '%lineage%' LOOP
        EXECUTE format('ALTER TABLE archive_sessions DROP CONSTRAINT %I', c);
    END LOOP;
    ALTER TABLE archive_sessions ADD CONSTRAINT archive_sessions_lineage_check
        CHECK (lineage IN ('', 'continuation', 'branch', 'delegated', 'reset'));
END
$$;
-- A conversation's lineage and parent are never forgotten: a later backup whose Hermes deleted the parent (it sets
-- the children's parent_session_id to NULL) or an older Hermes without the marker leaves them '' there, never here
-- (plan 1.4). Another non-empty value replaces it (Hermes re-links a conversation).
CREATE OR REPLACE FUNCTION archive_sessions_keep_lineage() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF coalesce(NEW.lineage, '') = '' AND coalesce(OLD.lineage, '') <> '' THEN
        NEW.lineage := OLD.lineage;
    END IF;
    IF coalesce(NEW.parent_source_session_id, '') = '' AND coalesce(OLD.parent_source_session_id, '') <> '' THEN
        NEW.parent_source_session_id := OLD.parent_source_session_id;
    END IF;
    RETURN NEW;
END
$fn$;
DROP TRIGGER IF EXISTS archive_sessions_keep_lineage ON archive_sessions;
CREATE TRIGGER archive_sessions_keep_lineage BEFORE UPDATE ON archive_sessions
    FOR EACH ROW EXECUTE FUNCTION archive_sessions_keep_lineage();

-- 3. Decisions, families and frozen conversations (plan 1.3) ----------------------------------------------------------
-- A JARVIS backup a decision was taken on (its SHA-256): what an import of that very file replays, and whether the
-- decisions on it were finished (Settle: settled_run_id).
CREATE TABLE IF NOT EXISTS brain_decision_backups (
    sha256            TEXT PRIMARY KEY,
    label             TEXT NOT NULL DEFAULT '',
    importer_version  TEXT NOT NULL DEFAULT '',
    newest_at         TIMESTAMPTZ,                   -- Hermes's time of the newest message in that backup
    first_run_id      BIGINT,                        -- the first import run of that backup here
    settled_run_id    BIGINT,                        -- the run that finished its decisions (NULL: not yet)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Family links between JARVIS conversations (a, b: JARVIS session ids), recorded the first time any import sees them
-- and never forgotten: append-only (a rollback and a restore never delete from it). Only kinds that copy rows; a
-- gateway reset (_reset_from) and a delegated sub-agent (_delegate_from) are never family links.
CREATE TABLE IF NOT EXISTS brain_session_links (
    a          TEXT NOT NULL,
    b          TEXT NOT NULL,
    how        TEXT NOT NULL CHECK (how IN ('parent', 'branched_from', 'compressed_from', 'shared_uid', 'copied_rows')),
    first_run  BIGINT,
    first_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (a, b, how)
);
CREATE INDEX IF NOT EXISTS brain_session_links_b_idx ON brain_session_links (b);
CREATE OR REPLACE FUNCTION brain_session_links_append_only() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'brain_session_links is append-only: a family link is never changed or forgotten';
END
$fn$;
DROP TRIGGER IF EXISTS brain_session_links_append_only ON brain_session_links;
CREATE TRIGGER brain_session_links_append_only BEFORE UPDATE OR DELETE ON brain_session_links
    FOR EACH ROW EXECUTE FUNCTION brain_session_links_append_only();
-- Conversations an admin stopped importing ("stop importing this conversation"): a later import skips them, counted.
CREATE TABLE IF NOT EXISTS brain_frozen_sessions (
    source_session_id  TEXT PRIMARY KEY,
    frozen_by          TEXT NOT NULL DEFAULT '',
    frozen_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. Purges: keyed fingerprints (HMAC, app/brain/identity.purge_fp) of purged imported items, never a plain hash ------
CREATE TABLE IF NOT EXISTS brain_purged_content (
    fp          TEXT PRIMARY KEY,                     -- 'k1:<key id>:<hex>'
    source_key  TEXT NOT NULL DEFAULT '',             -- the purge tombstone it belongs to ('' for an incoming version)
    kind        TEXT NOT NULL DEFAULT '',             -- file, memory_entry, role_block
    purged_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    purged_by   TEXT NOT NULL DEFAULT '',
    via         TEXT NOT NULL DEFAULT ''              -- purge, decisions-apply, import
);

-- Every purge, with or without a source key (a manual or a promoted item, an incoming version of a changed file): the
-- item's id and creation time, no text and no hash. restore.ps1 exports it, and decisions-apply --removals-only purges
-- again the very same item (same id and creation time) when a restore of an older dump brought it back.
CREATE TABLE IF NOT EXISTS brain_purged_items (
    item_id              BIGINT NOT NULL,
    item_created_at      TIMESTAMPTZ NOT NULL,
    origin               TEXT NOT NULL DEFAULT '',
    replaces_source_key  TEXT NOT NULL DEFAULT '',
    purged_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    purged_by            TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (item_id, item_created_at)  -- a restore resets the id sequence: an id names an item with its time
);

-- Runs again after restore.ps1 of an older dump: the restored rows and rollback copies need the full repair, and a
-- purge the restore undid (its tombstone is gone) keeps no fingerprint. The legacy mark stays.
DELETE FROM brain_maintenance WHERE name IN ('withhold_identity', 'purge_identity');
DELETE FROM brain_purged_content p
 WHERE p.source_key <> '' AND NOT EXISTS (SELECT 1 FROM brain_tombstones t WHERE t.source_key = p.source_key);

-- 5. Exact identity, and the guard: no code path can store text in a row a stone names --------------------------------
-- The JARVIS uid of a stored row: its source_uid, or the uid its key names ('' unknown).
CREATE OR REPLACE FUNCTION brain_row_uid(p_source_uid text, p_source_key text) RETURNS text
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT coalesce(nullif(p_source_uid, ''),
                    CASE WHEN p_source_key LIKE 'jarvis:message:uid:%' THEN substring(p_source_key FROM 20) END, '')
$fn$;
-- Whether a stone's (or a release's) binding and a row of the same JARVIS session and row id are the same JARVIS row
-- (identity.exact): the same role and tool_call_id (a binding whose role is not known matches any), the same time when
-- both have one, and never two different uids.
CREATE OR REPLACE FUNCTION brain_exact(t_role text, t_call text, t_at timestamptz, t_uid text,
                                       m_role text, m_call text, m_at timestamptz, m_uid text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $fn$
    SELECT (coalesce(t_role, '') = '' OR (t_role = coalesce(m_role, '') AND coalesce(t_call, '') = coalesce(m_call, '')))
       AND (t_at IS NULL OR m_at IS NULL OR t_at = m_at)
       AND NOT (coalesce(t_uid, '') <> '' AND coalesce(m_uid, '') <> '' AND t_uid <> m_uid)
$fn$;

CREATE OR REPLACE FUNCTION archive_messages_keep_withheld() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    v_via text;
BEGIN
    SELECT 'key' INTO v_via FROM brain_tombstones t
     WHERE t.source_key = NEW.source_key AND t.reason = 'withheld by an admin';
    IF v_via IS NULL AND EXISTS (SELECT 1 FROM brain_tombstones t
                                  WHERE t.source_row_id = NEW.source_message_id AND t.source_row_id <> ''
                                    AND t.reason = 'withheld by an admin') THEN
        -- the same JARVIS row: its exact identity (a reused row id, another time or another uid, is another message)
        SELECT 'same JARVIS row' INTO v_via
          FROM brain_tombstones t JOIN archive_sessions s ON s.source_session_id = t.source_session_id
         WHERE s.id = NEW.session_id AND t.source_row_id = NEW.source_message_id
           AND t.reason = 'withheld by an admin'
           AND brain_exact(t.role, t.tool_call_id, t.source_at, t.message_uid,
                           NEW.role, NEW.tool_call_id, NEW.at, brain_row_uid(NEW.source_uid, NEW.source_key))
         LIMIT 1;
    END IF;
    IF v_via IS NOT NULL THEN
        NEW.content := '';
        NEW.tool_calls := '[]'::jsonb;
        NEW.labels := '{}';
        NEW.truncated := false;
        NEW.withheld := true;
        NEW.source_fp := 'w1:' || NEW.state;
        NEW.flags := coalesce(NEW.flags, '{}'::jsonb) || jsonb_build_object('withheld_by_admin',
            coalesce(NEW.flags -> 'withheld_by_admin', '{}'::jsonb)
            || jsonb_build_object('note', 'withheld by an admin', 'guard', v_via));
    END IF;
    RETURN NEW;
END
$fn$;
DROP TRIGGER IF EXISTS archive_messages_keep_withheld ON archive_messages;
CREATE TRIGGER archive_messages_keep_withheld BEFORE INSERT OR UPDATE ON archive_messages
    FOR EACH ROW WHEN (NOT NEW.withheld) EXECUTE FUNCTION archive_messages_keep_withheld();

-- 6. The repair ----------------------------------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION brain_withhold_repair(p_cause text) RETURNS jsonb LANGUAGE plpgsql AS $fn$
DECLARE
    c_wh CONSTANT text := 'withheld by an admin';
    v_actor text := 'system (' || p_cause || ')';
    v_full boolean;
    v_watermark bigint;
    v_max_record bigint;
    n int;
    n_stones_added int := 0;
    n_key int := 0;
    n_row int := 0;
    n_fold int := 0;
    n_restricted int := 0;
    n_rows_fp int := 0;
    n_records_fp int := 0;
    n_records_blank int := 0;
    n_sessions int := 0;
    n_times int := 0;
    v_out jsonb;
BEGIN
    PERFORM set_config('client_min_messages', 'warning', true);
    -- this function is 0.6.2 code: its own writes pass brain_refuse_old_rows() (section 7), whoever calls it (the
    -- migration itself, run by a connection that may not say so)
    PERFORM set_config('cc.brain_purge_ledger', 'on', true);
    SELECT NOT EXISTS (SELECT 1 FROM brain_maintenance WHERE name = 'withhold_identity'),
           coalesce((SELECT (detail ->> 'records_to')::bigint FROM brain_maintenance WHERE name = 'withhold_identity'), 0)
      INTO v_full, v_watermark;
    v_max_record := coalesce((SELECT max(id) FROM import_records), 0);

    -- R4. Readable rows that are a withheld message. One row per hit; the first rule that names it wins.
    DROP TABLE IF EXISTS _withhold_hits;
    CREATE TEMP TABLE _withhold_hits (id bigint PRIMARY KEY, anchor text NOT NULL, via text NOT NULL,
                                      review text NOT NULL, after_run bigint NOT NULL) ON COMMIT DROP;
    -- a. the key (a key with a stone never names a readable row)
    INSERT INTO _withhold_hits
    SELECT m.id, CASE WHEN t.anchor_key = '' THEN t.source_key ELSE t.anchor_key END, 'key',
           CASE WHEN t.review = 'pending' THEN 'pending' ELSE '' END, t.after_run
      FROM brain_tombstones t JOIN archive_messages m ON m.source_key = t.source_key
     WHERE t.reason = c_wh AND NOT m.withheld
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS n_key = ROW_COUNT;
    -- b. the exact JARVIS row (session, row id, role, tool_call_id, time, uid): every row the importer stored it as;
    -- a hold's own row is held, a member's withheld
    INSERT INTO _withhold_hits
    SELECT DISTINCT ON (m.id) m.id, CASE WHEN t.anchor_key = '' THEN t.source_key ELSE t.anchor_key END,
           'same JARVIS row', CASE WHEN t.review = 'pending' THEN 'pending' ELSE '' END, t.after_run
      FROM brain_tombstones t
      JOIN archive_sessions s ON s.source_session_id = t.source_session_id
      JOIN archive_messages m ON m.session_id = s.id AND m.source_message_id = t.source_row_id
     WHERE t.reason = c_wh AND t.source_row_id <> '' AND NOT m.withheld
       AND brain_exact(t.role, t.tool_call_id, t.source_at, t.message_uid,
                       m.role, m.tool_call_id, m.at, brain_row_uid(m.source_uid, m.source_key))
     ORDER BY m.id, t.review = 'pending', t.source_key
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS n_row = ROW_COUNT;
    -- e. a fold that absorbed a withheld message (Hermes absorbed_message_uids: a compaction carrier, a
    -- micro-compaction merge) holds its text verbatim: a member, whatever its role. A fold's own uid is the survivor's
    -- message: fold stones are never uid sources.
    INSERT INTO _withhold_hits
    SELECT DISTINCT ON (m.id) m.id, CASE WHEN t.anchor_key = '' THEN t.source_key ELSE t.anchor_key END,
           'merged into another message', '', t.after_run
      FROM brain_tombstones t
      JOIN archive_messages m ON m.absorbed_uids @> ARRAY[t.message_uid]
     WHERE t.reason = c_wh AND t.review <> 'pending' AND t.message_uid <> '' AND NOT m.withheld
       AND t.via NOT IN ('merged into another message', 'upgrade: merged into another message')
     ORDER BY m.id, t.anchor_key <> '', t.source_key
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    n_fold := n;

    -- The rows found keep no text; their keys get a stone (binding from the row; never rebinding another stone).
    UPDATE archive_messages m
       SET content = '', tool_calls = '[]'::jsonb, labels = '{}', truncated = false, withheld = true,
           source_fp = 'w1:' || m.state,
           flags = m.flags || jsonb_build_object('withheld_by_admin',
                       coalesce(m.flags -> 'withheld_by_admin', '{}'::jsonb) || jsonb_build_object(
                           'note', 'withheld by an admin', 'via', h.via, 'repaired', p_cause,
                           'at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'),
                           'pending_copies', true)
                       || CASE WHEN h.review = 'pending' THEN '{"review": "pending"}'::jsonb ELSE '{}'::jsonb END)
      FROM _withhold_hits h
     WHERE m.id = h.id;
    INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason, anchor_key, via, review, after_run,
                                  source_session_id, source_row_id, role, tool_call_id, message_uid, uid_known, bound,
                                  source_at)
    SELECT m.source_key, now(), v_actor, c_wh, h.anchor, 'upgrade: ' || h.via, h.review, h.after_run,
           left(s.source_session_id, 200), left(m.source_message_id, 200), left(m.role, 40), left(m.tool_call_id, 200),
           left(brain_row_uid(m.source_uid, m.source_key), 200), false, true, m.at
      FROM _withhold_hits h JOIN archive_messages m ON m.id = h.id JOIN archive_sessions s ON s.id = m.session_id
     WHERE m.source_key <> ''
    ON CONFLICT (source_key) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    n_stones_added := n_stones_added + n;

    -- Their rollback copies lose the text too: only copies of the same JARVIS row (a copy of another row a key once
    -- named keeps its text: that text is readable in its own row). A copy keeps its sensitivity: one message is
    -- withheld, not restricted data in its conversation (a rollback that puts it back never makes the whole
    -- conversation restricted: review 2, S13).
    WITH blanked AS (
        UPDATE import_records x
           SET before = CASE WHEN x.before ? 'deleted_row' THEN
                            jsonb_set(x.before, '{deleted_row}', (x.before -> 'deleted_row')
                                || jsonb_build_object('content', '', 'tool_calls', '[]'::jsonb, 'labels', '[]'::jsonb,
                                                      'withheld', true, 'truncated', false,
                                                      'source_fp', 'w1:' || coalesce(x.before -> 'deleted_row' ->> 'state', 'live')))
                        ELSE x.before || jsonb_build_object('content', '', 'tool_calls', '[]'::jsonb, 'labels', '[]'::jsonb,
                                                             'withheld', true, 'truncated', false,
                                                             'source_fp', 'w1:' || coalesce(x.before ->> 'state', 'live'))
                        END
         WHERE x.record_type = 'message' AND x.before <> '{}'::jsonb
           AND (x.before ? 'content' OR x.before ? 'deleted_row')
           AND coalesce(x.before -> 'deleted_row' ->> 'withheld', x.before ->> 'withheld', 'false') <> 'true'
           AND EXISTS (SELECT 1 FROM _withhold_hits h JOIN archive_messages m ON m.id = h.id
                        WHERE (x.target_id = m.id OR x.source_key = m.source_key)
                          AND coalesce(x.before -> 'deleted_row' ->> 'source_message_id', x.before ->> 'source_message_id',
                                       m.source_message_id) = m.source_message_id)
        RETURNING 1
    )
    SELECT count(*) INTO n_records_blank FROM blanked;

    -- R5. Readable rows left in a conversation withheld for restricted data (0.6.x, finding 3), and their copies.
    WITH fixed AS (
        UPDATE archive_messages m
           SET content = '', tool_calls = '[]'::jsonb, labels = '{}', truncated = false, withheld = true,
               sensitivity = 'restricted', source_fp = 'w1:' || m.state,
               flags = m.flags || '{"withheld": "restricted data in this conversation", "withheld_pending_copies": true}'::jsonb
          FROM archive_sessions s
         WHERE s.id = m.session_id AND s.sensitivity = 'restricted' AND NOT m.withheld
        RETURNING m.id, m.session_id
    ), copies AS (
        UPDATE import_records x
           SET before = CASE WHEN x.before ? 'deleted_row' THEN
                            jsonb_set(x.before, '{deleted_row}', (x.before -> 'deleted_row')
                                || jsonb_build_object('content', '', 'tool_calls', '[]'::jsonb, 'labels', '[]'::jsonb,
                                                      'withheld', true, 'sensitivity', 'restricted', 'truncated', false,
                                                      'source_fp', 'w1:' || coalesce(x.before -> 'deleted_row' ->> 'state', 'live')))
                        ELSE x.before || jsonb_build_object('content', '', 'tool_calls', '[]'::jsonb, 'labels', '[]'::jsonb,
                                                             'withheld', true, 'sensitivity', 'restricted', 'truncated', false,
                                                             'source_fp', 'w1:' || coalesce(x.before ->> 'state', 'live'))
                        END
         WHERE x.record_type = 'message' AND x.before <> '{}'::jsonb AND (x.before ? 'content' OR x.before ? 'deleted_row')
           AND x.target_table = 'archive_messages' AND x.target_id IN (SELECT id FROM fixed)
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM fixed), (SELECT count(*) FROM copies) INTO n_restricted, n;
    n_records_blank := n_records_blank + n;
    -- Their sessions are recomputed by the start-up step; until then they show no summary.
    WITH touched AS (
        UPDATE archive_sessions s
           SET summary = '', flags = s.flags || '{"withhold_repair_pending": true}'::jsonb
         WHERE s.id IN (SELECT m.session_id FROM archive_messages m
                         WHERE m.withheld AND (m.flags -> 'withheld_by_admin' ? 'pending_copies'
                                               OR m.flags ? 'withheld_pending_copies'))
           AND NOT (s.flags ? 'withhold_repair_pending' AND s.summary = '')
        RETURNING 1
    )
    SELECT count(*) INTO n_sessions FROM touched;

    -- R6. No withheld row and no rollback copy of one keeps a text fingerprint (0.6.x kept a plain SHA-256). A released
    -- hold keeps source_fp '' (no fingerprint): it tells the next import to restore it from the source.
    UPDATE archive_messages SET source_fp = 'w1:' || state WHERE withheld AND source_fp NOT LIKE 'w1:%'
       AND NOT (source_fp = '' AND flags -> 'withheld_by_admin' ? 'released_at');
    GET DIAGNOSTICS n_rows_fp = ROW_COUNT;
    UPDATE import_records x
       SET before = CASE WHEN x.before ? 'deleted_row' THEN
                        jsonb_set(x.before, '{deleted_row,source_fp}',
                                  to_jsonb('w1:' || coalesce(x.before -> 'deleted_row' ->> 'state', 'live')))
                    ELSE jsonb_set(x.before, '{source_fp}', to_jsonb('w1:' || coalesce(x.before ->> 'state', 'live')))
                    END
     WHERE x.record_type = 'message' AND x.before <> '{}'::jsonb
       AND (v_full OR x.id > v_watermark
            OR x.target_id IN (SELECT h.id FROM _withhold_hits h))
       AND coalesce(x.before -> 'deleted_row' ->> 'withheld', x.before ->> 'withheld') = 'true'
       AND coalesce(x.before -> 'deleted_row' ->> 'source_fp', x.before ->> 'source_fp', 'w1:') NOT LIKE 'w1:%';
    GET DIAGNOSTICS n_records_fp = ROW_COUNT;

    -- R7. Every bound stone learns the time of the JARVIS row it names (a time, never text), from a stored row of it
    -- or, when an import deleted every one, from a rollback copy: Hermes keeps that time in each copy it makes. Only
    -- from that exact row (its role and tool_call_id, never another uid). Never changed once known. Last, so the stones
    -- R4 added above get theirs in this same run (a second run changes nothing).
    UPDATE brain_tombstones t SET source_at = m.at
      FROM archive_sessions s JOIN archive_messages m ON m.session_id = s.id
     WHERE t.reason = c_wh AND t.source_at IS NULL AND t.source_row_id <> ''
       AND s.source_session_id = t.source_session_id AND m.source_message_id = t.source_row_id
       AND m.at IS NOT NULL
       AND brain_exact(t.role, t.tool_call_id, NULL, t.message_uid,
                       m.role, m.tool_call_id, m.at, brain_row_uid(m.source_uid, m.source_key));
    GET DIAGNOSTICS n_times = ROW_COUNT;
    UPDATE brain_tombstones t SET source_at = (x.before -> 'deleted_row' ->> 'at')::timestamptz
      FROM archive_sessions s JOIN import_records x
           ON x.record_type = 'message' AND x.before ? 'deleted_row'
          AND (x.before -> 'deleted_row' ->> 'session_id') = s.id::text
     WHERE t.reason = c_wh AND t.source_at IS NULL AND t.source_row_id <> ''
       AND s.source_session_id = t.source_session_id
       AND (x.before -> 'deleted_row' ->> 'source_message_id') = t.source_row_id
       AND coalesce(x.before -> 'deleted_row' ->> 'at', '') ~ '^\d{4}-\d\d-\d\d'
       AND brain_exact(t.role, t.tool_call_id, NULL, t.message_uid,
                       coalesce(x.before -> 'deleted_row' ->> 'role', ''),
                       coalesce(x.before -> 'deleted_row' ->> 'tool_call_id', ''), NULL,
                       brain_row_uid(coalesce(x.before -> 'deleted_row' ->> 'source_uid', ''), x.source_key));
    GET DIAGNOSTICS n = ROW_COUNT;
    n_times := n_times + n;

    INSERT INTO brain_maintenance (name, done_at, detail)
    VALUES ('withhold_identity', now(), jsonb_build_object('records_to', v_max_record, 'cause', p_cause))
    ON CONFLICT (name) DO UPDATE SET done_at = EXCLUDED.done_at, detail = EXCLUDED.detail;

    v_out := jsonb_build_object(
        'stones_added', n_stones_added, 'withheld_by_key', n_key, 'withheld_by_jarvis_row', n_row,
        'withheld_by_fold', n_fold, 'restricted_rows_withheld', n_restricted,
        'fingerprints_removed', n_rows_fp + n_records_fp, 'rollback_copies_blanked', n_records_blank,
        'sessions_to_recompute', n_sessions, 'stone_times_learned', n_times, 'full', v_full);
    IF n_stones_added + n_key + n_row + n_fold + n_restricted + n_rows_fp + n_records_fp + n_records_blank > 0 THEN
        INSERT INTO audit_log (actor, action, target_type, target_id, detail)
        VALUES (v_actor, 'brain.withhold_identity_repaired', 'brain', '', v_out);
    END IF;
    RETURN v_out;
END
$fn$;

-- Read-only: what brain_withhold_repair() would change now and what is left for an admin (brain-import status,
-- scripts/brain-precheck.ps1). Counts only.
CREATE OR REPLACE FUNCTION brain_withhold_pending() RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    SELECT jsonb_build_object(
        'readable_by_key', (SELECT count(*) FROM archive_messages m JOIN brain_tombstones t ON t.source_key = m.source_key
                             WHERE t.reason = 'withheld by an admin' AND NOT m.withheld),
        'readable_by_jarvis_row', (SELECT count(DISTINCT m.id) FROM brain_tombstones t
                                     JOIN archive_sessions s ON s.source_session_id = t.source_session_id
                                     JOIN archive_messages m ON m.session_id = s.id AND m.source_message_id = t.source_row_id
                                    WHERE t.reason = 'withheld by an admin' AND t.source_row_id <> '' AND NOT m.withheld
                                      AND brain_exact(t.role, t.tool_call_id, t.source_at, t.message_uid, m.role,
                                                      m.tool_call_id, m.at, brain_row_uid(m.source_uid, m.source_key))),
        'readable_by_fold', (SELECT count(DISTINCT m.id) FROM brain_tombstones t
                               JOIN archive_messages m ON m.absorbed_uids @> ARRAY[t.message_uid]
                              WHERE t.reason = 'withheld by an admin' AND t.review <> 'pending' AND t.message_uid <> ''
                                AND t.via NOT IN ('merged into another message', 'upgrade: merged into another message')
                                AND NOT m.withheld),
        'readable_in_restricted_sessions', (SELECT count(*) FROM archive_messages m JOIN archive_sessions s ON s.id = m.session_id
                                             WHERE s.sensitivity = 'restricted' AND NOT m.withheld),
        'unbound_stones', (SELECT count(*) FROM brain_tombstones WHERE reason = 'withheld by an admin' AND NOT bound),
        'withheld_with_text_fingerprint', (SELECT count(*) FROM archive_messages WHERE withheld AND source_fp NOT LIKE 'w1:%'
                                                                               AND source_fp <> ''),
        -- held rows, not stones: a key adoption can leave two holds on one JARVIS row
        'held_for_review', (SELECT count(*) FROM (
                               SELECT DISTINCT coalesce(nullif(source_session_id, ''), source_key), source_row_id
                                 FROM brain_tombstones WHERE reason = 'withheld by an admin' AND review = 'pending') h),
        -- 0.6.2 plan 1.8: an admin's own withholds whose decision backup is not finished yet (Settle), or not known
        'unsettled_decisions', (SELECT count(*) FROM brain_tombstones t
                                 WHERE t.reason = 'withheld by an admin' AND t.anchor_key = '' AND t.review <> 'pending'
                                   AND NOT EXISTS (SELECT 1 FROM brain_decision_backups b
                                                    WHERE b.sha256 = t.decided_on AND b.settled_run_id IS NOT NULL)),
        -- holds that finishing decisions found and an admin has not looked at yet (identity.SETTLE_HOLD)
        'unreviewed_settle_holds', (SELECT count(*) FROM brain_tombstones
                                     WHERE reason = 'withheld by an admin' AND review = 'pending'
                                       AND via = 'found when finishing decisions'),
        -- rows of a later backup held in a withheld conversation family (identity.LATER_HOLD)
        'quarantined_rows', (SELECT count(*) FROM brain_tombstones
                              WHERE reason = 'withheld by an admin' AND review = 'pending'
                                AND via = 'new in a later backup'),
        'frozen_sessions', (SELECT count(*) FROM brain_frozen_sessions),
        'legacy_lock', EXISTS (SELECT 1 FROM brain_maintenance WHERE name = 'legacy_brain_data'),
        'last_tripwire', (SELECT detail FROM brain_maintenance WHERE name = 'last_tripwire'),
        'repair_done', EXISTS (SELECT 1 FROM brain_maintenance WHERE name = 'withhold_identity'));
$fn$;

-- 7. An older importer is refused on this schema. 0.6.0 (importer 1.0.0) and 0.6.1 (1.0.1) know nothing of the stones'
-- bindings: run on this database (back to 0.6.1 code), they would store Hermes's live copy of a withheld message with its
-- text under a new key and row id that neither the guard above nor the repair can tell from an unrelated row. Their
-- start_run inserts the run first, so the refusal comes before any change.
CREATE OR REPLACE FUNCTION brain_refuse_old_importer() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    v int[];
BEGIN
    v := string_to_array(substring(NEW.importer_version FROM '^\d+\.\d+\.\d+'), '.')::int[];
    IF v IS NOT NULL AND v < ARRAY[1, 1, 0] THEN
        RAISE EXCEPTION 'this database holds 0.6.2 withholds (migration 0008_brain_withhold_identity): import with the '
                        '0.6.2 importer (1.1.0 or later); importer % is refused', NEW.importer_version;
    END IF;
    RETURN NEW;
END
$fn$;
DROP TRIGGER IF EXISTS brain_refuse_old_importer ON import_runs;
CREATE TRIGGER brain_refuse_old_importer BEFORE INSERT ON import_runs
    FOR EACH ROW EXECUTE FUNCTION brain_refuse_old_importer();
-- ... and its rows (review 3, rv3-dur-2): code older than 0.6.2 (an import that was already running, a 0.6.0/0.6.1
-- rollback) cannot write an archived message on this schema; every 0.6.2 connection says it is 0.6.2 or later
-- (app/db.py sets cc.brain_purge_ledger = 'on'). Nor can any connection store a message for a run of an older importer
-- that is still running (its run row predates this file): such a row is that importer's, whoever writes it. A row of a
-- finished older run that a 0.6.2 rollback puts back stays allowed. pg_restore loads the data before it creates
-- triggers; TRUNCATE (the tests) fires no row trigger.
CREATE OR REPLACE FUNCTION brain_refuse_old_rows() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF coalesce(current_setting('cc.brain_purge_ledger', true), '') <> 'on' THEN
        RAISE EXCEPTION 'this database holds 0.6.2 withholds (migration 0008_brain_withhold_identity): code older '
                        'than 0.6.2 cannot store archived JARVIS messages here; import again with 0.6.2';
    END IF;
    IF NEW.import_run_id IS NOT NULL
       AND (TG_OP = 'INSERT' OR NEW.import_run_id IS DISTINCT FROM OLD.import_run_id)
       AND EXISTS (SELECT 1 FROM import_runs r WHERE r.id = NEW.import_run_id AND r.status = 'running'
                                                 AND brain_importer_older(r.importer_version)) THEN
        RAISE EXCEPTION 'this database holds 0.6.2 withholds (migration 0008_brain_withhold_identity): import run % '
                        'is by an importer older than 1.1.0; its messages are refused (import again with 0.6.2)',
                        NEW.import_run_id;
    END IF;
    RETURN NEW;
END
$fn$;
DROP TRIGGER IF EXISTS brain_refuse_old_rows ON archive_messages;
CREATE TRIGGER brain_refuse_old_rows BEFORE INSERT OR UPDATE ON archive_messages
    FOR EACH ROW EXECUTE FUNCTION brain_refuse_old_rows();

-- 8. A purge by older code is refused on this schema (review 2, S22). 0.6.0 and 0.6.1 purge with a tombstone only: no
-- purge ledger row (brain_purged_items) and no keyed fingerprint (brain_purged_content), which is what keeps a purge
-- through a restore of an older backup and a renamed file. Going back to 0.6.1 with the data kept is supported, so its
-- code can reach this database: removing a deleted item for good is refused unless the connection says it keeps
-- those records (app/db.py sets cc.brain_purge_ledger = 'on' on every connection of 0.6.2 and later). The deleted item
-- stays deleted (no reader lists it) until it is purged with 0.6.2. TRUNCATE (the tests) fires no row trigger.
CREATE OR REPLACE FUNCTION brain_refuse_old_purge() RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
    IF OLD.deleted_at IS NOT NULL AND coalesce(current_setting('cc.brain_purge_ledger', true), '') <> 'on' THEN
        RAISE EXCEPTION 'this database keeps 0.6.2 purge records (migration 0008_brain_withhold_identity): purge with '
                        'Command Center 0.6.2 or later; the deleted item stays deleted until then';
    END IF;
    RETURN OLD;
END
$fn$;
DROP TRIGGER IF EXISTS brain_refuse_old_purge ON memory_items;
CREATE TRIGGER brain_refuse_old_purge BEFORE DELETE ON memory_items
    FOR EACH ROW EXECUTE FUNCTION brain_refuse_old_purge();

SELECT brain_withhold_repair('migration');
