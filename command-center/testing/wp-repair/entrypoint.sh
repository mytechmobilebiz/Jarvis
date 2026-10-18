#!/bin/sh
# Disposable repair-test server: WordPress (PHP built-in server on 8080, as
# www-data), MariaDB in the wp-repair-db container, OpenSSH on 22 with cc-gate.
set -eu
DOCROOT=/var/www/html
as_www() { su -s /bin/sh -c "$1" www-data; }

if [ ! -e "$DOCROOT/wp-includes/version.php" ]; then
  mkdir -p "$DOCROOT"
  cp -a /usr/src/wordpress/. "$DOCROOT/"
fi
chown -R www-data:www-data "$DOCROOT"
cd "$DOCROOT"
if [ ! -f wp-config.php ]; then
  as_www "wp config create --dbname=wordpress --dbuser=wordpress --dbpass='$WP_DB_PASSWORD' --dbhost=wp-repair-db --skip-check --quiet"
fi
i=0
until as_www "wp db query 'SELECT 1' --quiet" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 60 ] || { echo "database not reachable" >&2; exit 1; }; sleep 2
done
if ! as_www "wp core is-installed" 2>/dev/null; then
  pass=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
  as_www "wp core install --url=http://wp-repair:8080 --title='Disposable repair test' --admin_user=testadmin --admin_password='$pass' --admin_email=admin@example.test --skip-email --quiet"
fi

# cc-gate exactly as the owner would install it on a CWP server, with this site.
mkdir -p /etc/cc-gate
cat > /etc/cc-gate/config.json <<JSON
{
  "allow_changes": true,
  "sites": {"wp-repair": {"path": "$DOCROOT", "user": "www-data", "url": "http://wp-repair:8080"}},
  "services": ["php-web"],
  "restartable_services": ["php-web"],
  "wp_cli": "/usr/local/bin/wp",
  "sudo": "/usr/bin/sudo",
  "systemctl": "/usr/local/bin/systemctl",
  "site_helper": "/usr/local/lib/cc-gate/cc-site-helper",
  "backup_dir": "/var/lib/cc-gate/backups",
  "log_file": "/var/log/cc-gate/gate.log"
}
JSON
chmod 0644 /etc/cc-gate/config.json
ssh-keygen -A >/dev/null
sh /opt/cc-gate/install-gate.sh

/usr/sbin/sshd.pam -E /var/log/sshd.log
echo "repair-test server ready: http://wp-repair:8080, ssh ccagent@wp-repair"
exec su -s /bin/sh -c "exec php -S 0.0.0.0:8080 -t $DOCROOT" www-data
