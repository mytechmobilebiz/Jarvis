#!/bin/sh
# Runs once, when the PostgreSQL volume is first created: one database and one
# login role each for the Command Center and for n8n, so neither can read the
# other's tables.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v cc_password="$CC_DB_PASSWORD" -v n8n_password="$N8N_DB_PASSWORD" <<'SQL'
CREATE ROLE command_center LOGIN PASSWORD :'cc_password';
CREATE DATABASE command_center OWNER command_center;
REVOKE ALL ON DATABASE command_center FROM PUBLIC;

CREATE ROLE n8n LOGIN PASSWORD :'n8n_password';
CREATE DATABASE n8n OWNER n8n;
REVOKE ALL ON DATABASE n8n FROM PUBLIC;
SQL
