-- 0.6.2: durable withholding (docs/JARVIS_BRAIN_SPEC.md 2.9).
--
-- An admin's withhold is bound to the identity of the JARVIS message, not only to the importer's derived source_key.
-- Every part of that identity is one more 'withheld by an admin' row in brain_tombstones (a "stone"): its key, plus a
-- binding (JARVIS session id, JARVIS row id, role, tool_call_id, message_uid), the admin's own stone it belongs to
-- (anchor_key), how it joined (via) and whether an admin still has to look at it (review). 0.6.1 code reads these rows
-- as before (by key), so it keeps working on this schema.
--
-- Idempotent, and safe wherever 0006_brain and 0007_brain_withhold ran: a fresh database and 0.5.0 (0006, 0007, 0008
-- run in one transaction), 0.6.0, 0.6.1, and restore.ps1 of an older dump (the restored tables come back without
-- these columns and without this file's schema_migrations row, so it runs again; the DELETE below re-arms the full
-- repair). No foreign key into a table older than 0.6.0 (0006_brain.sql, top).
--
-- What it does to existing data, inside the migration transaction (no lock and no key needed), through
-- brain_withhold_repair('migration'), which the start-up check, every import, rescan --yes and brain-import repair
-- call again (it is idempotent):
--   R1 a stone for every row an admin withheld that has none (0.6.0);
--   R2 every stone gets the JARVIS row it named when the admin withheld it (C2: the first update after the withhold
--      says which row that was, not the row the key names today);
--   R3 every key an import recorded as a carried-on copy or an adjacent repeat of a row of the message gets a stone;
--   R4 readable rows that are the same message are withheld: by key, by JARVIS row, by uid (held for review), and,
--      for text 0.6.1 let come back, by 0.6.1's own plain source_fp in the same conversation or a continuation or
--      branch of it, stored after the withhold (held for review; never a row whose own uid key says it is another
--      message);
--   R5 readable rows left in a restricted conversation are withheld (finding 3);
--   R6 withheld rows and their rollback copies keep no text fingerprint: source_fp becomes 'w1:<state>';
--   their rollback copies are blanked, their sessions marked for recompute, one audit entry with counts.
-- The start-up step (app/brain/identity.repair) then empties promoted copies, scrubs quotes and recomputes the
-- marked sessions. A BEFORE INSERT OR UPDATE trigger keeps every row a stone names without text, whatever code
-- writes it.

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

-- An admin's release of a row held for review: the uid and same-text rules no longer hold it (the certain rules,
-- key, JARVIS row, carried copy, still do).
CREATE TABLE IF NOT EXISTS brain_withhold_exemptions (
    anchor_key         TEXT NOT NULL,
    source_session_id  TEXT NOT NULL,
    source_row_id      TEXT NOT NULL,
    reason             TEXT NOT NULL DEFAULT 'released by an admin',
    decided_by         TEXT NOT NULL DEFAULT '',
    decided_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (anchor_key, source_session_id, source_row_id)
);

-- 2. Archive: the JARVIS uid of a stored row, and the indexes the identity rules use --------------------------------
ALTER TABLE archive_messages ADD COLUMN IF NOT EXISTS source_uid TEXT NOT NULL DEFAULT '';
UPDATE archive_messages SET source_uid = substring(source_key FROM 20)
 WHERE source_uid = '' AND source_key LIKE 'jarvis:message:uid:%';
CREATE INDEX IF NOT EXISTS archive_messages_source_row_idx ON archive_messages (session_id, source_message_id);
CREATE INDEX IF NOT EXISTS archive_messages_source_uid_idx ON archive_messages (source_uid) WHERE source_uid <> '';
CREATE INDEX IF NOT EXISTS archive_sessions_parent_idx ON archive_sessions (parent_source_session_id)
    WHERE parent_source_session_id <> '';

-- 3. Purges: keyed fingerprints (HMAC, app/brain/identity.purge_fp) of purged imported items, never a plain hash ------
CREATE TABLE IF NOT EXISTS brain_purged_content (
    fp          TEXT PRIMARY KEY,                     -- 'k1:<key id>:<hex>'
    source_key  TEXT NOT NULL DEFAULT '',             -- the purge tombstone it belongs to ('' for an incoming version)
    kind        TEXT NOT NULL DEFAULT '',             -- file, memory_entry, role_block
    purged_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    purged_by   TEXT NOT NULL DEFAULT '',
    via         TEXT NOT NULL DEFAULT ''              -- purge, decisions-apply, import
);

-- 4. One-time steps that are done, and how far the rollback copies were converted ---------------------------------
CREATE TABLE IF NOT EXISTS brain_maintenance (
    name     TEXT PRIMARY KEY,
    done_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail   JSONB NOT NULL DEFAULT '{}'::jsonb
);
-- Runs again after restore.ps1 of an older dump: the restored rows and rollback copies need the full repair, and a
-- purge the restore undid (its tombstone is gone) keeps no fingerprint.
DELETE FROM brain_maintenance WHERE name IN ('withhold_identity', 'purge_identity');
DELETE FROM brain_purged_content p
 WHERE p.source_key <> '' AND NOT EXISTS (SELECT 1 FROM brain_tombstones t WHERE t.source_key = p.source_key);

-- 5. The guard: no code path can store text in a row a stone names ------------------------------------------------
CREATE OR REPLACE FUNCTION archive_messages_keep_withheld() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    v_via text;
BEGIN
    SELECT 'key' INTO v_via FROM brain_tombstones t
     WHERE t.source_key = NEW.source_key AND t.reason = 'withheld by an admin';
    IF v_via IS NULL AND EXISTS (SELECT 1 FROM brain_tombstones t
                                  WHERE t.source_row_id = NEW.source_message_id AND t.source_row_id <> ''
                                    AND t.reason = 'withheld by an admin') THEN
        SELECT 'same JARVIS row' INTO v_via
          FROM brain_tombstones t JOIN archive_sessions s ON s.source_session_id = t.source_session_id
         WHERE s.id = NEW.session_id AND t.source_row_id = NEW.source_message_id
           AND t.reason = 'withheld by an admin' AND t.role IN ('', NEW.role)
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
    c_carried CONSTANT text := 'carried-on copy of a compacted message';
    c_repeat CONSTANT text := 'same as the previous message';
    c_row_key CONSTANT text := '^jarvis:message:(?!uid:)(.+):([0-9]+)$';
    v_actor text := 'system (' || p_cause || ')';
    v_full boolean;
    v_watermark bigint;
    v_max_record bigint;
    st record;
    v_after bigint;
    v_ids bigint[];
    v_row jsonb;
    v_sess bigint;
    v_sid text;
    v_rowid text;
    v_uid text;
    n int;
    n_stones_added int := 0;
    n_bound int := 0;
    n_dup int := 0;
    n_key int := 0;
    n_row int := 0;
    n_uid int := 0;
    n_text int := 0;
    n_restricted int := 0;
    n_rows_fp int := 0;
    n_records_fp int := 0;
    n_records_blank int := 0;
    n_sessions int := 0;
    v_out jsonb;
BEGIN
    PERFORM set_config('client_min_messages', 'warning', true);
    SELECT NOT EXISTS (SELECT 1 FROM brain_maintenance WHERE name = 'withhold_identity'),
           coalesce((SELECT (detail ->> 'records_to')::bigint FROM brain_maintenance WHERE name = 'withhold_identity'), 0)
      INTO v_full, v_watermark;
    v_max_record := coalesce((SELECT max(id) FROM import_records), 0);

    -- R1. A stone for every row an admin withheld that has none (0.6.0 withheld without one; a stone lost in a restore).
    -- Never for a hold an admin released (released_at): its stone was deleted on purpose, the next import restores it.
    INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason, via)
    SELECT m.source_key,
           coalesce(CASE WHEN (m.flags -> 'withheld_by_admin' ->> 'at') ~ '^\d{4}-\d\d-\d\dT'
                         THEN (m.flags -> 'withheld_by_admin' ->> 'at')::timestamptz END, now()),
           v_actor, c_wh, 'upgrade'
      FROM archive_messages m
     WHERE m.withheld AND m.flags ? 'withheld_by_admin' AND m.source_key <> ''
       AND NOT (m.flags -> 'withheld_by_admin' ? 'released_at')
    ON CONFLICT (source_key) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    n_stones_added := n_stones_added + n;

    -- R2. Bind every unbound stone to the JARVIS row it named when the admin withheld it.
    FOR st IN SELECT * FROM brain_tombstones WHERE reason = c_wh AND NOT bound ORDER BY source_key LOOP
        v_after := CASE WHEN st.after_run > 0 THEN st.after_run
                        ELSE coalesce((SELECT max(u.id) FROM import_runs u WHERE u.started_at <= st.purged_at), 0) END;
        v_ids := ARRAY(SELECT m.id FROM archive_messages m WHERE m.source_key = st.source_key
                       UNION
                       SELECT x.target_id FROM import_records x
                        WHERE x.source_key = st.source_key AND x.record_type = 'message'
                          AND x.target_table = 'archive_messages' AND x.target_id IS NOT NULL
                          AND x.action IN ('imported', 'withheld', 'updated'));
        v_row := NULL;
        v_sess := NULL;
        -- (i) the row as it was when withheld: the first update of it by a run that started after the withhold
        SELECT coalesce(x.before -> 'deleted_row', x.before),
               coalesce((x.before -> 'deleted_row' ->> 'session_id')::bigint,
                        (SELECT m.session_id FROM archive_messages m WHERE m.id = x.target_id),
                        (SELECT (y.before -> 'deleted_row' ->> 'session_id')::bigint FROM import_records y
                          WHERE y.target_table = 'archive_messages' AND y.target_id = x.target_id
                            AND y.before ? 'deleted_row' LIMIT 1))
          INTO v_row, v_sess
          FROM import_records x
         WHERE x.target_table = 'archive_messages' AND x.target_id = ANY (v_ids) AND x.action = 'updated'
           AND x.run_id > v_after AND (x.before ? 'source_message_id' OR x.before ? 'deleted_row')
         ORDER BY x.id LIMIT 1;
        -- (ii) the row that holds the key now
        IF v_row IS NULL THEN
            SELECT to_jsonb(m) - 'search', m.session_id INTO v_row, v_sess
              FROM archive_messages m WHERE m.source_key = st.source_key;
        END IF;
        -- (iii) the newest copy of a row of it that an import deleted
        IF v_row IS NULL THEN
            SELECT x.before -> 'deleted_row', (x.before -> 'deleted_row' ->> 'session_id')::bigint INTO v_row, v_sess
              FROM import_records x
             WHERE x.target_table = 'archive_messages' AND x.target_id = ANY (v_ids) AND x.before ? 'deleted_row'
             ORDER BY x.id DESC LIMIT 1;
        END IF;
        v_sid := (SELECT s.source_session_id FROM archive_sessions s WHERE s.id = v_sess);
        IF v_sid IS NULL AND v_sess IS NOT NULL THEN  -- the archive session is gone: its import record names it
            v_sid := (SELECT substring(x.source_key FROM 16) FROM import_records x
                       WHERE x.record_type = 'session' AND x.target_table = 'archive_sessions'
                         AND x.target_id = v_sess AND x.source_key LIKE 'jarvis:session:%' LIMIT 1);
        END IF;
        -- (iv) the key itself
        v_sid := coalesce(v_sid, (regexp_match(st.source_key, c_row_key))[1], '');
        v_rowid := coalesce(v_row ->> 'source_message_id', (regexp_match(st.source_key, c_row_key))[2], '');
        v_uid := coalesce(nullif(v_row ->> 'source_uid', ''),
                          CASE WHEN st.source_key LIKE 'jarvis:message:uid:%' THEN substring(st.source_key FROM 20) END,
                          '');
        UPDATE brain_tombstones
           SET source_session_id = left(v_sid, 200), source_row_id = left(v_rowid, 200),
               role = left(coalesce(v_row ->> 'role', ''), 40), tool_call_id = left(coalesce(v_row ->> 'tool_call_id', ''), 200),
               message_uid = left(v_uid, 200), uid_known = (st.source_key LIKE 'jarvis:message:uid:%'),
               after_run = v_after, bound = (v_rowid <> '' OR v_uid <> '')
         WHERE source_key = st.source_key;
        IF v_rowid <> '' OR v_uid <> '' THEN  -- a stone with no evidence at all (no message key) stays unbound, uncounted
            n_bound := n_bound + 1;
        END IF;
    END LOOP;

    -- R3. Keys an import recorded as a carried-on copy or an adjacent repeat of a row of a withheld message, to a
    -- fixed point (a copy that came back as a row of its own can have copies too).
    LOOP
        WITH stoned AS (
            SELECT t.source_key, CASE WHEN t.anchor_key = '' THEN t.source_key ELSE t.anchor_key END AS anchor,
                   t.role, t.after_run, t.purged_at
              FROM brain_tombstones t WHERE t.reason = c_wh
        ), held AS (
            SELECT m.id AS row_id, m.session_id, sd.* FROM stoned sd JOIN archive_messages m ON m.source_key = sd.source_key
            UNION
            SELECT x.target_id, coalesce(m.session_id, (x.before -> 'deleted_row' ->> 'session_id')::bigint), sd.*
              FROM stoned sd JOIN import_records x ON x.source_key = sd.source_key
              LEFT JOIN archive_messages m ON m.id = x.target_id
             WHERE x.record_type = 'message' AND x.target_table = 'archive_messages' AND x.target_id IS NOT NULL
               AND x.action IN ('imported', 'withheld', 'updated')
        )
        INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason, anchor_key, via, after_run,
                                      source_session_id, source_row_id, role, message_uid, uid_known, bound)
        SELECT DISTINCT ON (d.source_key) d.source_key, h.purged_at, v_actor, c_wh, h.anchor, 'duplicate', h.after_run,
               left(coalesce((regexp_match(d.source_key, c_row_key))[1], s.source_session_id, ''), 200),
               left(coalesce((regexp_match(d.source_key, c_row_key))[2], ''), 200),
               h.role,
               CASE WHEN d.source_key LIKE 'jarvis:message:uid:%' THEN left(substring(d.source_key FROM 20), 200) ELSE '' END,
               false, true
          FROM import_records d
          JOIN held h ON h.row_id = d.target_id
          LEFT JOIN archive_sessions s ON s.id = h.session_id
         WHERE d.record_type = 'message' AND d.action = 'duplicate' AND d.target_table = 'archive_messages'
           AND d.reason IN (c_carried, c_repeat)
           AND NOT EXISTS (SELECT 1 FROM brain_tombstones t WHERE t.source_key = d.source_key)
         ORDER BY d.source_key, h.anchor
        ON CONFLICT (source_key) DO NOTHING;
        GET DIAGNOSTICS n = ROW_COUNT;
        n_dup := n_dup + n;
        n_stones_added := n_stones_added + n;
        EXIT WHEN n = 0;
    END LOOP;

    -- R4. Readable rows that are the same message. One row per hit; the first rule that names it wins (certain first).
    DROP TABLE IF EXISTS _withhold_hits;
    CREATE TEMP TABLE _withhold_hits (id bigint PRIMARY KEY, anchor text NOT NULL, via text NOT NULL,
                                      review text NOT NULL, after_run bigint NOT NULL) ON COMMIT DROP;
    -- a. the key (a key with a stone never names a readable row)
    INSERT INTO _withhold_hits
    SELECT m.id, CASE WHEN t.anchor_key = '' THEN t.source_key ELSE t.anchor_key END, 'key', '', t.after_run
      FROM brain_tombstones t JOIN archive_messages m ON m.source_key = t.source_key
     WHERE t.reason = c_wh AND NOT m.withheld
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS n_key = ROW_COUNT;
    -- b. the JARVIS row (session + row id; the role when known): every row the importer stored it as
    INSERT INTO _withhold_hits
    SELECT DISTINCT ON (m.id) m.id, CASE WHEN t.anchor_key = '' THEN t.source_key ELSE t.anchor_key END,
           'same JARVIS row', '', t.after_run
      FROM brain_tombstones t
      JOIN archive_sessions s ON s.source_session_id = t.source_session_id
      JOIN archive_messages m ON m.session_id = s.id AND m.source_message_id = t.source_row_id
     WHERE t.reason = c_wh AND t.source_row_id <> '' AND t.role IN ('', m.role) AND NOT m.withheld
     ORDER BY m.id, t.review, t.source_key
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS n_row = ROW_COUNT;
    -- c. the uid (held for review: SQL cannot compare the text, the next import does)
    INSERT INTO _withhold_hits
    SELECT DISTINCT ON (m.id) m.id, CASE WHEN t.anchor_key = '' THEN t.source_key ELSE t.anchor_key END,
           'same message id', 'pending', t.after_run
      FROM brain_tombstones t
      JOIN archive_messages m ON m.source_uid = t.message_uid
      JOIN archive_sessions s ON s.id = m.session_id
     WHERE t.reason = c_wh AND t.message_uid <> '' AND t.role IN ('', m.role) AND NOT m.withheld
       AND NOT EXISTS (SELECT 1 FROM brain_withhold_exemptions e
                        WHERE e.anchor_key = CASE WHEN t.anchor_key = '' THEN t.source_key ELSE t.anchor_key END
                          AND e.source_session_id = s.source_session_id AND e.source_row_id = m.source_message_id)
     ORDER BY m.id, t.source_key
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS n_uid = ROW_COUNT;
    -- d. text 0.6.x let come back: 0.6.1's own plain source_fp of the withheld message (on its withheld row, on a
    -- rollback copy of a row of it), equal on a readable row of the same conversation or of a continuation or branch
    -- of it, stored or changed by a run that started after the withhold. Never a row whose own uid key or uid says
    -- it is another Hermes message when the withheld message's uid is known. Held for review.
    WITH RECURSIVE grp AS (
        SELECT DISTINCT CASE WHEN t.anchor_key = '' THEN t.source_key ELSE t.anchor_key END AS anchor, t.source_key,
               t.source_session_id, t.source_row_id
          FROM brain_tombstones t WHERE t.reason = c_wh AND t.source_session_id <> '' AND t.source_row_id <> ''
    ), anchor_info AS (
        SELECT a.source_key AS anchor, a.after_run,
               CASE WHEN a.uid_known THEN a.message_uid ELSE '' END AS known_uid
          FROM brain_tombstones a WHERE a.reason = c_wh AND a.anchor_key = ''
    ), plain AS (       -- the withheld message's plain fingerprints, wherever 0.6.x left one
        SELECT g.anchor, g.source_session_id, m.source_fp AS fp
          FROM grp g JOIN archive_messages m ON m.source_key = g.source_key
         WHERE m.withheld AND m.source_fp <> '' AND m.source_fp NOT LIKE 'w1:%'
           AND m.source_message_id = g.source_row_id              -- not a row the key was moved to (C2)
        UNION
        SELECT g.anchor, g.source_session_id, coalesce(x.before -> 'deleted_row' ->> 'source_fp', x.before ->> 'source_fp')
          FROM grp g JOIN import_records x ON x.source_key = g.source_key
         WHERE x.record_type = 'message' AND x.before <> '{}'::jsonb
           AND coalesce(x.before -> 'deleted_row' ->> 'source_message_id', x.before ->> 'source_message_id')
               = g.source_row_id
           AND coalesce(x.before -> 'deleted_row' ->> 'withheld', x.before ->> 'withheld') = 'true'
           AND coalesce(x.before -> 'deleted_row' ->> 'source_fp', x.before ->> 'source_fp', '') NOT IN ('')
           AND coalesce(x.before -> 'deleted_row' ->> 'source_fp', x.before ->> 'source_fp') NOT LIKE 'w1:%'
    ), cand AS (         -- readable rows with that fingerprint, anywhere (few: a fingerprint covers the whole row)
        SELECT DISTINCT p.anchor, p.source_session_id AS anchor_sid, m.id, m.session_id
          FROM plain p JOIN archive_messages m ON m.source_fp = p.fp
         WHERE NOT m.withheld
    ), up(id, anchor, anchor_sid, sid, lineage, parent) AS (
        -- walk up from each candidate's conversation through continuation and branch parents until the withheld
        -- message's conversation is reached: no depth limit, and UNION stops at a cycle
        SELECT c.id, c.anchor, c.anchor_sid, s.source_session_id, s.lineage, s.parent_source_session_id
          FROM cand c JOIN archive_sessions s ON s.id = c.session_id
        UNION
        SELECT u.id, u.anchor, u.anchor_sid, p.source_session_id, p.lineage, p.parent_source_session_id
          FROM up u JOIN archive_sessions p ON p.source_session_id = u.parent
         WHERE u.sid <> u.anchor_sid AND u.lineage IN ('continuation', 'branch') AND u.parent <> ''
    ), in_family AS (
        SELECT DISTINCT id, anchor FROM up WHERE sid = anchor_sid
    )
    INSERT INTO _withhold_hits
    SELECT DISTINCT ON (m.id) m.id, f.anchor, 'same text', 'pending', ai.after_run
      FROM in_family f
      JOIN anchor_info ai ON ai.anchor = f.anchor
      JOIN archive_messages m ON m.id = f.id
      JOIN archive_sessions s ON s.id = m.session_id
     WHERE NOT m.withheld
       AND (coalesce(m.import_run_id, 0) > ai.after_run
            OR EXISTS (SELECT 1 FROM import_records x WHERE x.target_table = 'archive_messages' AND x.target_id = m.id
                          AND x.action = 'updated' AND x.run_id > ai.after_run))
       AND (ai.known_uid = ''                                      -- the withheld message's uid is unknown
            OR coalesce(nullif(m.source_uid, ''), CASE WHEN m.source_key LIKE 'jarvis:message:uid:%'
                                                        THEN substring(m.source_key FROM 20) END, '')
               IN ('', ai.known_uid)                                  -- no uid of its own, or the same one
            OR (s.lineage IN ('continuation', 'branch') AND m.at IS NOT NULL AND s.started_at IS NOT NULL
                AND m.at <= s.started_at))       -- copied history of a branch or continuation: a fresh uid says nothing
       AND NOT EXISTS (SELECT 1 FROM brain_withhold_exemptions e WHERE e.anchor_key = f.anchor
                          AND e.source_session_id = s.source_session_id AND e.source_row_id = m.source_message_id)
    ORDER BY m.id, f.anchor
    ON CONFLICT (id) DO NOTHING;
    GET DIAGNOSTICS n_text = ROW_COUNT;

    -- The rows found keep no text; their keys get a stone (binding from the row; never rebinding another stone).
    UPDATE archive_messages m
       SET content = '', tool_calls = '[]'::jsonb, labels = '{}', truncated = false, withheld = true,
           source_fp = 'w1:' || m.state,
           flags = m.flags || jsonb_build_object('withheld_by_admin',
                       coalesce(m.flags -> 'withheld_by_admin', '{}'::jsonb) || jsonb_build_object(
                           'note', 'withheld by an admin', 'via', h.via, 'repaired', p_cause,
                           'at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'),
                           'pending_copies', true))
      FROM _withhold_hits h
     WHERE m.id = h.id;
    INSERT INTO brain_tombstones (source_key, purged_at, purged_by, reason, anchor_key, via, review, after_run,
                                  source_session_id, source_row_id, role, tool_call_id, message_uid, uid_known, bound)
    SELECT m.source_key, now(), v_actor, c_wh, h.anchor, 'upgrade: ' || h.via, h.review, h.after_run,
           left(s.source_session_id, 200), left(m.source_message_id, 200), left(m.role, 40), left(m.tool_call_id, 200),
           left(coalesce(nullif(m.source_uid, ''),
                         CASE WHEN m.source_key LIKE 'jarvis:message:uid:%' THEN substring(m.source_key FROM 20) END, ''), 200),
           false, true
      FROM _withhold_hits h JOIN archive_messages m ON m.id = h.id JOIN archive_sessions s ON s.id = m.session_id
     WHERE m.source_key <> ''
    ON CONFLICT (source_key) DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT;
    n_stones_added := n_stones_added + n;

    -- Their rollback copies lose the text too: only copies of the same JARVIS row (a copy of another row a key once
    -- named, C3, keeps its text: that text is readable in its own row).
    WITH blanked AS (
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

    INSERT INTO brain_maintenance (name, done_at, detail)
    VALUES ('withhold_identity', now(), jsonb_build_object('records_to', v_max_record, 'cause', p_cause))
    ON CONFLICT (name) DO UPDATE SET done_at = EXCLUDED.done_at, detail = EXCLUDED.detail;

    v_out := jsonb_build_object(
        'stones_added', n_stones_added, 'stones_bound', n_bound, 'duplicate_keys', n_dup,
        'withheld_by_key', n_key, 'withheld_by_jarvis_row', n_row, 'held_by_message_id', n_uid,
        'held_by_same_text', n_text, 'restricted_rows_withheld', n_restricted,
        'fingerprints_removed', n_rows_fp + n_records_fp, 'rollback_copies_blanked', n_records_blank,
        'sessions_to_recompute', n_sessions, 'full', v_full);
    IF n_stones_added + n_bound + n_key + n_row + n_uid + n_text + n_restricted + n_rows_fp + n_records_fp
       + n_records_blank > 0 THEN
        INSERT INTO audit_log (actor, action, target_type, target_id, detail)
        VALUES (v_actor, 'brain.withhold_identity_repaired', 'brain', '', v_out);
    END IF;
    RETURN v_out;
END
$fn$;

-- Read-only: what brain_withhold_repair() would change now (brain-import status, scripts/brain-precheck.ps1).
CREATE OR REPLACE FUNCTION brain_withhold_pending() RETURNS jsonb LANGUAGE sql STABLE AS $fn$
    SELECT jsonb_build_object(
        'readable_by_key', (SELECT count(*) FROM archive_messages m JOIN brain_tombstones t ON t.source_key = m.source_key
                             WHERE t.reason = 'withheld by an admin' AND NOT m.withheld),
        'readable_by_jarvis_row', (SELECT count(DISTINCT m.id) FROM brain_tombstones t
                                     JOIN archive_sessions s ON s.source_session_id = t.source_session_id
                                     JOIN archive_messages m ON m.session_id = s.id AND m.source_message_id = t.source_row_id
                                    WHERE t.reason = 'withheld by an admin' AND t.source_row_id <> ''
                                      AND t.role IN ('', m.role) AND NOT m.withheld),
        'readable_in_restricted_sessions', (SELECT count(*) FROM archive_messages m JOIN archive_sessions s ON s.id = m.session_id
                                             WHERE s.sensitivity = 'restricted' AND NOT m.withheld),
        'unbound_stones', (SELECT count(*) FROM brain_tombstones WHERE reason = 'withheld by an admin' AND NOT bound),
        'withheld_with_text_fingerprint', (SELECT count(*) FROM archive_messages WHERE withheld AND source_fp NOT LIKE 'w1:%'
                                                                               AND source_fp <> ''),
        'held_for_review', (SELECT count(*) FROM brain_tombstones WHERE reason = 'withheld by an admin' AND review = 'pending'),
        'repair_done', EXISTS (SELECT 1 FROM brain_maintenance WHERE name = 'withhold_identity'));
$fn$;

SELECT brain_withhold_repair('migration');
