#!/bin/sh
# Starts n8n with the Command Center's workflows and credentials provisioned.
#
# 1. The owner account is managed by environment (n8n's
#    N8N_INSTANCE_OWNER_* settings); its bcrypt hash is computed here from
#    N8N_OWNER_PASSWORD so .env holds one plain secret like the others.
# 2. The two credentials the workflows use (the Command Center service token
#    and the webhook secret) are written from the environment to a temporary
#    file, imported (n8n encrypts them with N8N_ENCRYPTION_KEY) and deleted.
# 3. The workflows in /opt/cc/workflows are imported by fixed id, so a restart
#    updates them in place rather than duplicating them, and published.
set -eu

NODE_MODULES=/usr/local/lib/node_modules/n8n/node_modules

: "${N8N_OWNER_PASSWORD:?N8N_OWNER_PASSWORD must be set}"
: "${CC_SERVICE_TOKEN:?CC_SERVICE_TOKEN must be set}"
: "${N8N_WEBHOOK_SECRET:?N8N_WEBHOOK_SECRET must be set}"

N8N_INSTANCE_OWNER_PASSWORD_HASH="$(node -e "process.stdout.write(require('$NODE_MODULES/bcryptjs').hashSync(process.env.N8N_OWNER_PASSWORD, 10))")"
export N8N_INSTANCE_OWNER_MANAGED_BY_ENV=true N8N_INSTANCE_OWNER_PASSWORD_HASH
unset N8N_OWNER_PASSWORD

umask 077
CREDS="$(mktemp)"
trap 'rm -f "$CREDS"' EXIT
node /opt/cc/provision-credentials.mjs > "$CREDS"
n8n import:credentials --input="$CREDS"
rm -f "$CREDS"
unset CC_SERVICE_TOKEN N8N_WEBHOOK_SECRET

# Workflows: only when the files changed since the last start (a re-import over
# an active workflow is refused). Unpublish ours, import them by fixed id, then
# publish the ones whose JSON says "active"; n8n 2.x activates published
# workflows at start-up. Workflows created in the editor are left alone.
workflow_ids() {
  node -e '
    const fs = require("fs"), dir = "/opt/cc/workflows";
    for (const f of fs.readdirSync(dir).filter((f) => f.endsWith(".json")).sort()) {
      const wf = JSON.parse(fs.readFileSync(`${dir}/${f}`, "utf8"));
      if (process.argv[1] !== "active" || wf.active) console.log(wf.id);
    }' "$1"
}
STAMP=/home/node/.n8n/cc-workflows.sha256
CURRENT="$(cat /opt/cc/workflows/*.json | sha256sum | cut -d' ' -f1)"
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$CURRENT" ]; then
  for id in $(workflow_ids all); do
    n8n unpublish:workflow --id="$id" >/dev/null 2>&1 || true
  done
  n8n import:workflow --separate --input=/opt/cc/workflows
  for id in $(workflow_ids active); do
    n8n publish:workflow --id="$id"
  done
  echo "$CURRENT" > "$STAMP"
else
  echo "Command Center workflows unchanged; skipping import."
fi

exec /docker-entrypoint.sh start
