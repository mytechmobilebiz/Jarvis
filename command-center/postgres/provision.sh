#!/bin/sh
# Run by the one-shot db-init service on every start. Idempotent: creates any
# missing login role and database (one each for the Command Center, n8n and
# the isolated Credit Repair store), keeps role passwords in step with .env,
# and makes sure no role can connect to another's database.
set -eu

export PGPASSWORD="$POSTGRES_PASSWORD"
psql -v ON_ERROR_STOP=1 -h postgres -U postgres -d postgres \
  -v cc_pw="$CC_DB_PASSWORD" -v n8n_pw="$N8N_DB_PASSWORD" -v cr_pw="$CREDIT_REPAIR_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', r)
  FROM unnest(ARRAY['command_center', 'n8n', 'credit_repair']) AS r
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) \gexec
ALTER ROLE command_center PASSWORD :'cc_pw';
ALTER ROLE n8n PASSWORD :'n8n_pw';
ALTER ROLE credit_repair PASSWORD :'cr_pw';
-- The app's errors are logged without DETAIL: a refused row ("Failing row contains ...") can be imported or customer
-- text. Superuser-only, so set here, for this role's sessions only.
ALTER ROLE command_center SET log_error_verbosity = 'terse';
SELECT format('CREATE DATABASE %I OWNER %I', d, d)
  FROM unnest(ARRAY['command_center', 'n8n', 'credit_repair']) AS d
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = d) \gexec
REVOKE ALL ON DATABASE command_center FROM PUBLIC;
REVOKE ALL ON DATABASE n8n FROM PUBLIC;
REVOKE ALL ON DATABASE credit_repair FROM PUBLIC;
SQL
echo "databases ready: command_center, n8n, credit_repair"
