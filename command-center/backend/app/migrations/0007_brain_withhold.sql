-- 0.6.1: memory_item_versions.change also allows 'quarantined' (written by store.quarantine_item). In 0.6.0 the
-- CHECK did not allow it, so withholding a message that had a promoted copy failed and was rolled back.
--
-- Idempotent and safe wherever 0006_brain ran:
--   - fresh database / upgrade from 0.5.0: 0006 and 0007 run in the same transaction;
--   - 0.6.0 database: only this file runs;
--   - restore.ps1 of a pre-0.6.0 dump: the brain tables stay, both rows leave schema_migrations, both files run again;
--   - restore.ps1 of a 0.6.0 dump: the table comes back with the old CHECK and no 0007 row; this file runs again.
-- The CHECK on `change` is found by its column, whatever its name, and replaced. The new list is a superset of
-- 0.6.0's, so every existing row passes and 0.6.0 code keeps working on this schema. No foreign key, no data change:
-- the two indexes at the end only make 0.6.1's withhold rules fast (0.6.0 code ignores them).

DO $$
DECLARE
    con record;
BEGIN
    FOR con IN
        SELECT c.conname
          FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attname = 'change' AND NOT a.attisdropped
         WHERE c.conrelid = 'memory_item_versions'::regclass
           AND c.contype = 'c'
           AND c.conkey = ARRAY[a.attnum]
    LOOP
        EXECUTE format('ALTER TABLE memory_item_versions DROP CONSTRAINT %I', con.conname);
    END LOOP;
END
$$;

ALTER TABLE memory_item_versions
    ADD CONSTRAINT memory_item_versions_change_check
    CHECK (change IN ('created', 'imported', 'edited', 'approved', 'rejected', 'archived', 'reopened', 'deleted',
                      'restored', 'redacted', 'reverted', 'quarantined'));

-- A memory item promoted by 0.6.0 names its message only by its source_ref (the session id and the position,
-- policy.PROMOTED_REF): agents and the Brain page find such a copy of a withheld message by that session id
-- (app/brain/policy.py).
CREATE INDEX IF NOT EXISTS archive_sessions_source_session_idx ON archive_sessions (source_session_id);
-- The history answers and reference notes that quote a removed message or item (app/brain/quotes.py), found without
-- reading every task. jsonb_path_ops serves the @> lookups the removal makes.
CREATE INDEX IF NOT EXISTS tasks_quotes_idx ON tasks
    USING GIN ((result -> 'data' -> 'sources') jsonb_path_ops, (result -> 'data' -> 'memory_notes') jsonb_path_ops);
