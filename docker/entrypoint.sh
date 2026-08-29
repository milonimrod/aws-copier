#!/bin/sh
# Creates (or reuses) a user matching PUID/PGID, then execs the CMD as that user via gosu.
# This keeps files the daemon writes on NAS bind mounts (e.g. .milo_backup.info) owned by a
# real, non-root UID/GID matching the NAS share's owner, instead of root.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if ! getent group "${PGID}" >/dev/null 2>&1; then
    groupadd -g "${PGID}" appgroup
fi
group_name="$(getent group "${PGID}" | cut -d: -f1)"

if ! getent passwd "${PUID}" >/dev/null 2>&1; then
    useradd -u "${PUID}" -g "${group_name}" -M -s /usr/sbin/nologin appuser
fi
user_name="$(getent passwd "${PUID}" | cut -d: -f1)"

exec gosu "${user_name}:${group_name}" "$@"
