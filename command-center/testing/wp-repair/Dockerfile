# A DISPOSABLE WordPress with a real OpenSSH server, sudo and WP-CLI, for
# testing cc-gate and the repair workflows end to end. Test overlay only:
# never deployed, not in the Windows package, never pointed at a real site.
#
# The sandbox this was built in cannot reach Debian or Alpine package mirrors,
# so the pieces come from images of the same Alpine release (3.24): OpenSSH and
# sudo from linuxserver/openssh-server, Python from python:3.12-alpine, PHP and
# WP-CLI from wordpress:cli, and WordPress itself from wordpress:6-apache.
ARG REGISTRY=docker.io
FROM ${REGISTRY}/linuxserver/openssh-server:latest AS ssh
FROM ${REGISTRY}/library/python:3.12-alpine AS py
FROM ${REGISTRY}/library/wordpress:6-apache AS wpsrc
FROM ${REGISTRY}/library/wordpress:cli

USER root
COPY --from=ssh /usr/sbin/sshd.pam /usr/sbin/sshd.pam
COPY --from=ssh /usr/lib/ssh/ /usr/lib/ssh/
COPY --from=ssh /usr/bin/ssh-keygen /usr/bin/ssh-keygen
COPY --from=ssh /usr/lib/libpam.so.0 /usr/lib/libpam.so.0.85.1 /usr/lib/
COPY --from=ssh /usr/bin/sudo /usr/bin/sudo
COPY --from=ssh /usr/sbin/visudo /usr/sbin/visudo
COPY --from=ssh /usr/lib/sudo/ /usr/lib/sudo/
COPY --from=py /usr/local/bin/python3.12 /usr/local/bin/python3
COPY --from=py /usr/local/lib/python3.12/ /usr/local/lib/python3.12/
COPY --from=py /usr/local/lib/libpython3.12.so.1.0 /usr/local/lib/
COPY --from=wpsrc /usr/src/wordpress/ /usr/src/wordpress/
COPY server/ /opt/cc-gate/
COPY testing/wp-repair/ /opt/wp-repair/
RUN set -e; chown root:root /usr/bin/sudo && chmod 4755 /usr/bin/sudo \
 && (id sshd >/dev/null 2>&1 || adduser -S -D -H -h /var/empty -s /sbin/nologin sshd) \
 && mkdir -p /var/empty /etc/ssh /etc/sudoers.d \
 && printf 'root ALL=(ALL:ALL) ALL\n@includedir /etc/sudoers.d\n' > /etc/sudoers && chmod 0440 /etc/sudoers \
 && install -m 0644 /opt/wp-repair/sshd_config /etc/ssh/sshd_config \
 && install -m 0755 /opt/wp-repair/systemctl /usr/local/bin/systemctl \
 && install -m 0755 /opt/wp-repair/cc-test-authorize /opt/wp-repair/cc-test-tamper /usr/local/bin/ \
 && chmod 0755 /opt/wp-repair/entrypoint.sh \
 && python3 -c "import json, shlex, subprocess, pwd; print('python ok')" \
 && sudo -V | head -1 \
 && ssh-keygen -A && /usr/sbin/sshd.pam -t && rm -f /etc/ssh/ssh_host_*
EXPOSE 22 8080
ENTRYPOINT ["/opt/wp-repair/entrypoint.sh"]
