#!/bin/sh
# Installs the throwaway test WordPress (test overlay only). Idempotent.
set -eu
cd /var/www/html
for i in $(seq 1 60); do
  [ -f wp-config.php ] && wp db check >/dev/null 2>&1 && break
  sleep 2
done
if wp core is-installed 2>/dev/null; then
  echo "test WordPress already installed"
  exit 0
fi
wp core install \
  --url="http://wp-test" \
  --title="Command Center Test Site" \
  --admin_user="testadmin" \
  --admin_password="test-only-$(date +%s)" \
  --admin_email="test@example.invalid" \
  --skip-email
wp rewrite structure '/%postname%/' --hard
echo "test WordPress installed: $(wp core version)"
