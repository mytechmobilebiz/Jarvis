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
#   - installs cc-gate, cc-wp (runs WP-CLI in a clean environment) and
#     cc-site-helper, owned by root;
#   - creates /etc/cc-gate/config.json from the example if there is none;
#   - writes /etc/sudoers.d/cc-gate from the configuration (validated with
#     visudo first): run cc-wp and the site helper as each configured site's
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
WP_RUNNER=$HELPER_DIR/cc-wp
CONF_DIR=/etc/cc-gate
CONF=$CONF_DIR/config.json
SUDOERS=/etc/sudoers.d/cc-gate
LOG_DIR=/var/log/cc-gate

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
NL=$(printf '\n_'); NL=${NL%_}; CR=$(printf '\r')
# Exactly one line: the name goes into the sudo rules, where a second line would be a rule of its own.
case $ACCOUNT in *"$NL"*|*"$CR"*|*\\*) echo "invalid account name" >&2; exit 1 ;; esac
printf '%s\n' "$ACCOUNT" | grep -Eq '^[a-z_][a-z0-9_-]{0,31}$' || { echo "invalid account name: $ACCOUNT" >&2; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
cd /  # nothing below may pick up files from the directory this was started in

if [ "$MODE" = remove ]; then
  rm -f "$SUDOERS"
  HOME_DIR=$(getent passwd "$ACCOUNT" 2>/dev/null | cut -d: -f6 || true)
  [ -n "$HOME_DIR" ] && rm -f "$HOME_DIR/.ssh/authorized_keys"
  rm -f "$GATE" "$HELPER" "$WP_RUNNER"
  echo "Removed the sudo rules, the authorized key and the cc-gate programs."
  echo "Kept: $CONF, $LOG_DIR and the backup folder. To delete the account too: userdel -r $ACCOUNT"
  exit 0
fi

# Python 3.6+ (EL8/EL9 always have /usr/libexec/platform-python).
PY=""
for candidate in /usr/libexec/platform-python /usr/bin/python3 /usr/local/bin/python3; do
  if [ -x "$candidate" ] && "$candidate" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then
    PY=$candidate; break
  fi
done
[ -n "$PY" ] || { echo "Python 3.6 or newer is required (dnf install python3)" >&2; exit 1; }

if [ "$MODE" = install ]; then
  if [ -n "$PUBKEY" ]; then
    # Exactly one line: a second line in authorized_keys would be a key without the forced command.
    case $PUBKEY in *"$NL"*|*"$CR"*|*\\*) echo "the public key must be one 'ssh-ed25519 AAAA... comment' line" >&2; exit 1 ;; esac
    printf '%s\n' "$PUBKEY" | grep -Eq '^ssh-ed25519 [A-Za-z0-9+/=]{60,}( [A-Za-z0-9@._-]{1,64})?$' \
      || { echo "the public key must be one 'ssh-ed25519 AAAA... comment' line" >&2; exit 1; }
  fi
  if ! id "$ACCOUNT" >/dev/null 2>&1; then
    if command -v useradd >/dev/null 2>&1; then
      useradd -m -s /bin/sh "$ACCOUNT"
    else
      adduser -D -s /bin/sh "$ACCOUNT"
    fi
    # No password, but not locked (some sshd setups refuse key logins to locked accounts).
    # Only for an account created here: an existing account keeps its password.
    if command -v usermod >/dev/null 2>&1; then usermod -p '*' "$ACCOUNT"; else sed -i "s/^$ACCOUNT:![^:]*:/$ACCOUNT:*:/" /etc/shadow; fi
    echo "Created account $ACCOUNT"
  fi
  [ "$(id -u "$ACCOUNT")" != 0 ] || { echo "$ACCOUNT has uid 0; refusing" >&2; exit 1; }

  mkdir -p "$HELPER_DIR" "$CONF_DIR"
  for pair in "cc-gate:$GATE" "cc-site-helper:$HELPER" "cc-wp:$WP_RUNNER"; do
    src=$HERE/${pair%%:*}; dst=${pair#*:}
    tmp=$(mktemp)
    # -I: isolated mode (no current directory, PYTHON* variables or user packages).
    { echo "#!$PY -I"; tail -n +2 "$src"; } > "$tmp"
    install -o root -g root -m 0755 "$tmp" "$dst"
    rm -f "$tmp"
  done
  if [ ! -f "$CONF" ]; then
    install -o root -g root -m 0644 "$HERE/config.example.json" "$CONF"
    echo "Created $CONF from the example: edit the sites, then run: sh $0 --sync-sites"
  fi
  chown root:root "$CONF_DIR" "$CONF"; chmod 0755 "$CONF_DIR"; chmod 0644 "$CONF"
  mkdir -p "$LOG_DIR"
  chown "$ACCOUNT" "$LOG_DIR"; chmod 0750 "$LOG_DIR"

  if [ -n "$PUBKEY" ]; then
    HOME_DIR=$(getent passwd "$ACCOUNT" | cut -d: -f6)
    SSH_DIR=$HOME_DIR/.ssh
    # Root writes here: refuse links and anything the account does not own.
    for f in "$SSH_DIR" "$SSH_DIR/authorized_keys"; do
      if [ -L "$f" ]; then echo "$f is a symbolic link; refusing" >&2; exit 1; fi
      if [ -e "$f" ] && [ "$(stat -c %u "$f")" != "$(id -u "$ACCOUNT")" ]; then echo "$f is not owned by $ACCOUNT; refusing" >&2; exit 1; fi
    done
    mkdir -p "$SSH_DIR"; chown "$ACCOUNT" "$SSH_DIR"; chmod 0700 "$SSH_DIR"
    tmp=$(mktemp)
    printf 'command="%s",restrict %s\n' "$GATE" "$PUBKEY" > "$tmp"
    install -o "$ACCOUNT" -m 0600 "$tmp" "$SSH_DIR/authorized_keys"
    rm -f "$tmp"
    echo "Installed the Command Center key for $ACCOUNT (forced command: $GATE)"
  fi
fi

# The backup folder from the configuration (absolute path, no "..").
BACKUP_DIR=$("$PY" -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("backup_dir", "/var/lib/cc-gate/backups"))' "$CONF")
case $BACKUP_DIR in
  /*) ;; *) echo "backup_dir must be an absolute path" >&2; exit 1 ;;
esac
case $BACKUP_DIR in
  *..*|*[!A-Za-z0-9._/-]*) echo "backup_dir may only contain letters, digits and . _ - /, and no '..'" >&2; exit 1 ;;
esac
mkdir -p "$BACKUP_DIR"
chown root:root "$BACKUP_DIR"; chmod 0755 "$BACKUP_DIR"

# --- per-site backup folders and the sudo rules, from the configuration
PAIRS=$("$PY" -I -c '
import json, re, sys
c = json.load(open(sys.argv[1]))
for key, s in sorted(c.get("sites", {}).items()):
    if re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", key) and re.match(r"^[a-z_][a-z0-9_.-]{0,31}$", s.get("user", "")):
        print(key, s["user"])
' "$CONF")
ACCOUNT_UID=$(id -u "$ACCOUNT" 2>/dev/null) || { echo "account $ACCOUNT does not exist; run the install first" >&2; exit 1; }
[ "$ACCOUNT_UID" != 0 ] || { echo "$ACCOUNT has uid 0; refusing" >&2; exit 1; }
# The gate account holds the sudo rules for every site, so a site whose PHP runs as it (by name or uid)
# could use them to run WP-CLI as every other site's user. Not a pipeline, so that exit ends the script.
while read -r key user; do
  [ -n "$key" ] && id "$user" >/dev/null 2>&1 || continue
  if [ "$user" = "$ACCOUNT" ] || [ "$(id -u "$user")" = "$ACCOUNT_UID" ]; then
    rm -f "$SUDOERS"
    echo "site $key runs as the gate account $ACCOUNT; give the gate its own account. The sudo rules were removed until then." >&2
    exit 1
  fi
done <<EOF
$PAIRS
EOF
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
SYSTEMCTL=$("$PY" -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("systemctl", "/usr/bin/systemctl"))' "$CONF")
case $SYSTEMCTL in
  /*) ;; *) echo "systemctl must be an absolute path" >&2; exit 1 ;;
esac
case $SYSTEMCTL in
  *[!A-Za-z0-9._/-]*) echo "systemctl may only contain letters, digits and . _ - /" >&2; exit 1 ;;
esac
RESTARTABLE=$("$PY" -I -c '
import json, re, sys
c = json.load(open(sys.argv[1]))
print(" ".join(s for s in c.get("restartable_services", []) if re.match(r"^[a-z0-9][a-z0-9@._-]{0,63}$", s) and s in c.get("services", [])))
' "$CONF")

TMP_SUDO=$(mktemp)
{
  printf '%s\n' "# Written by install-gate.sh. $ACCOUNT can only reach these rules through cc-gate (SSH forced command)."
  printf '%s\n' "Defaults:$ACCOUNT !requiretty"
  if [ -n "$SITE_USERS" ]; then
    printf '%s\n' "$ACCOUNT ALL=($SITE_USERS) NOPASSWD: $WP_RUNNER, $HELPER"
  fi
  for svc in $RESTARTABLE; do
    printf '%s\n' "$ACCOUNT ALL=(root) NOPASSWD: $SYSTEMCTL restart $svc"
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
echo "  changes allowed: $("$PY" -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("allow_changes") is True)' "$CONF") (allow_changes in $CONF)"
echo "  restartable services: ${RESTARTABLE:-none}"
echo
echo "Host key fingerprints to compare with the Command Center (Servers page):"
for f in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ecdsa_key.pub /etc/ssh/ssh_host_rsa_key.pub; do
  [ -f "$f" ] && ssh-keygen -lf "$f" || true
done
echo
echo "If CSF or another firewall restricts SSH, allow the Command Center's public IP address."
