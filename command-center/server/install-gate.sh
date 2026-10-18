#!/bin/sh
# Installs (or updates, or removes) cc-gate on a server, e.g. a CWP server.
# Run it as root ON THE SERVER, yourself. The Command Center never runs it.
#
#   sh install-gate.sh --public-key "ssh-ed25519 AAAA... infinite-command-center"
#   sh install-gate.sh --sync-sites       # after editing /etc/cc-gate/config.json
#   sh install-gate.sh --remove           # take the Command Center's access away again
#
# What it does (nothing else):
#   - creates the unprivileged account (default "ccagent") if missing; it gets
#     no password and no sudo rights beyond the rules below;
#   - installs cc-gate and cc-site-helper, owned by root;
#   - creates /etc/cc-gate/config.json from the example if there is none;
#   - writes /etc/sudoers.d/cc-gate from the configuration (validated with
#     visudo first): run WP-CLI and the site helper as each configured site's
#     user, and restart only the services you list as restartable;
#   - installs the Command Center's public key with command="cc-gate",restrict,
#     so the key can do nothing but run cc-gate (no shell, no forwarding);
#   - prints the server's host key fingerprints for the Command Center to pin.
set -eu

ACCOUNT=ccagent
PUBKEY=""
MODE=install
GATE=/usr/local/bin/cc-gate
HELPER_DIR=/usr/local/lib/cc-gate
HELPER=$HELPER_DIR/cc-site-helper
CONF_DIR=/etc/cc-gate
CONF=$CONF_DIR/config.json
SUDOERS=/etc/sudoers.d/cc-gate
LOG_DIR=/var/log/cc-gate
BACKUP_DIR=/var/lib/cc-gate/backups

usage() { sed -n '2,8p' "$0"; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --user) ACCOUNT=${2:-}; shift 2 ;;
    --public-key) PUBKEY=${2:-}; shift 2 ;;
    --sync-sites) MODE=sync; shift ;;
    --remove) MODE=remove; shift ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "run this as root on the server" >&2; exit 1; }
case "$ACCOUNT" in
  root|"") echo "the account must not be root" >&2; exit 1 ;;
esac
echo "$ACCOUNT" | grep -Eq '^[a-z_][a-z0-9_-]{0,31}$' || { echo "invalid account name: $ACCOUNT" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)

if [ "$MODE" = remove ]; then
  rm -f "$SUDOERS"
  HOME_DIR=$(getent passwd "$ACCOUNT" 2>/dev/null | cut -d: -f6 || true)
  [ -n "$HOME_DIR" ] && rm -f "$HOME_DIR/.ssh/authorized_keys"
  rm -f "$GATE" "$HELPER"
  echo "Removed the sudo rules, the authorized key and the cc-gate programs."
  echo "Kept: $CONF, $LOG_DIR and $BACKUP_DIR. To delete the account too: userdel -r $ACCOUNT"
  exit 0
fi

# Python 3.6+ (EL8/EL9 always have /usr/libexec/platform-python).
PY=""
for candidate in /usr/libexec/platform-python /usr/bin/python3 /usr/local/bin/python3; do
  if [ -x "$candidate" ] && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
    PY=$candidate; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.6 or newer is required (dnf install python3)" >&2; exit 1; }

if [ "$MODE" = install ]; then
  if ! id "$ACCOUNT" >/dev/null 2>&1; then
    if command -v useradd >/dev/null 2>&1; then
      useradd -m -s /bin/sh "$ACCOUNT"
    else
      adduser -D -s /bin/sh "$ACCOUNT"
    fi
    echo "Created account $ACCOUNT"
  fi
  # No password, but not locked (some sshd setups refuse key logins to locked accounts).
  if command -v usermod >/dev/null 2>&1; then usermod -p '*' "$ACCOUNT"; else sed -i "s/^$ACCOUNT:![^:]*:/$ACCOUNT:*:/" /etc/shadow; fi
  [ "$(id -u "$ACCOUNT")" != 0 ] || { echo "$ACCOUNT has uid 0; refusing" >&2; exit 1; }

  mkdir -p "$HELPER_DIR" "$CONF_DIR"
  for pair in "cc-gate:$GATE" "cc-site-helper:$HELPER"; do
    src=$HERE/${pair%%:*}; dst=${pair#*:}
    tmp=$(mktemp)
    { echo "#!$PY"; tail -n +2 "$src"; } > "$tmp"
    install -o root -g root -m 0755 "$tmp" "$dst"
    rm -f "$tmp"
  done
  if [ ! -f "$CONF" ]; then
    install -o root -g root -m 0644 "$HERE/config.example.json" "$CONF"
    echo "Created $CONF from the example: edit the sites, then run: sh $0 --sync-sites"
  fi
  chown root:root "$CONF_DIR" "$CONF"; chmod 0755 "$CONF_DIR"; chmod 0644 "$CONF"
  mkdir -p "$LOG_DIR" "$BACKUP_DIR"
  chown "$ACCOUNT" "$LOG_DIR"; chmod 0750 "$LOG_DIR"
  chown root:root "$BACKUP_DIR"; chmod 0755 "$BACKUP_DIR"

  if [ -n "$PUBKEY" ]; then
    echo "$PUBKEY" | grep -Eq '^ssh-ed25519 [A-Za-z0-9+/=]{60,}( [A-Za-z0-9@._-]{1,64})?$' \
      || { echo "the public key must be one 'ssh-ed25519 AAAA... comment' line" >&2; exit 1; }
    HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)
    mkdir -p "$HOME_DIR/.ssh"
    echo "command=\"$GATE\",restrict $PUBKEY" > "$HOME_DIR/.ssh/authorized_keys"
    chown -R "$ACCOUNT" "$HOME_DIR/.ssh"; chmod 0700 "$HOME_DIR/.ssh"; chmod 0600 "$HOME_DIR/.ssh/authorized_keys"
    echo "Installed the Command Center key for $ACCOUNT (forced command: $GATE)"
  fi
fi

# --- per-site backup folders and the sudo rules, from the configuration
PAIRS=$("$PY" -c '
import json, re, sys
c = json.load(open(sys.argv[1]))
for key, s in sorted(c.get("sites", {}).items()):
    if re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", key) and re.match(r"^[a-z_][a-z0-9_.-]{0,31}$", s.get("user", "")):
        print(key, s["user"])
' "$CONF")
SITE_USERS=""
for_each_site() {
  echo "$PAIRS" | while read -r key user; do
    [ -n "$key" ] || continue
    if ! id "$user" >/dev/null 2>&1; then echo "warning: site $key: user $user does not exist; skipped" >&2; continue; fi
    [ "$(id -u "$user")" != 0 ] || { echo "warning: site $key runs as root; skipped" >&2; continue; }
    mkdir -p "$BACKUP_DIR/$key"; chown "$user" "$BACKUP_DIR/$key"; chmod 0700 "$BACKUP_DIR/$key"
    echo "$user"
  done
}
SITE_USERS=$(for_each_site | sort -u | tr '\n' ',' | sed 's/,$//')
WP_CLI=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("wp_cli", "/usr/local/bin/wp"))' "$CONF")
SYSTEMCTL=$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("systemctl", "/usr/bin/systemctl"))' "$CONF")
RESTARTABLE=$("$PY" -c '
import json, re, sys
c = json.load(open(sys.argv[1]))
print(" ".join(s for s in c.get("restartable_services", []) if re.match(r"^[a-z0-9][a-z0-9@._-]{0,63}$", s) and s in c.get("services", [])))
' "$CONF")

TMP_SUDO=$(mktemp)
{
  echo "# Written by install-gate.sh. $ACCOUNT can only reach these rules through cc-gate (SSH forced command)."
  echo "Defaults:$ACCOUNT !requiretty"
  if [ -n "$SITE_USERS" ]; then
    echo "$ACCOUNT ALL=($SITE_USERS) NOPASSWD: $WP_CLI, $HELPER"
  fi
  for svc in $RESTARTABLE; do
    echo "$ACCOUNT ALL=(root) NOPASSWD: $SYSTEMCTL restart $svc"
  done
} > "$TMP_SUDO"
if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$TMP_SUDO" >/dev/null || { echo "generated sudo rules did not validate; nothing changed" >&2; rm -f "$TMP_SUDO"; exit 1; }
else
  echo "visudo not found; refusing to install sudo rules without validation" >&2; rm -f "$TMP_SUDO"; exit 1
fi
mkdir -p /etc/sudoers.d
install -o root -g root -m 0440 "$TMP_SUDO" "$SUDOERS"
rm -f "$TMP_SUDO"

echo
echo "cc-gate is installed for account $ACCOUNT."
echo "  sites: ${SITE_USERS:+(as users $SITE_USERS)} $(echo "$PAIRS" | awk '{print $1}' | tr '\n' ' ')"
echo "  changes allowed: $("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("allow_changes") is True)' "$CONF") (allow_changes in $CONF)"
echo "  restartable services: ${RESTARTABLE:-none}"
echo
echo "Host key fingerprints to compare with the Command Center (Servers page):"
for f in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ecdsa_key.pub /etc/ssh/ssh_host_rsa_key.pub; do
  [ -f "$f" ] && ssh-keygen -lf "$f" || true
done
echo
echo "If CSF or another firewall restricts SSH, allow the Command Center's public IP address."
