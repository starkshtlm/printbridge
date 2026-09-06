#!/bin/sh
# Start as root only long enough to settle who owns the data directory, then drop privileges.
#
# `./data` is a bind mount, so its owner is whatever uid the operator happens to have — 1000 on
# most machines, something else on a NAS. Two rules keep that painless:
#
#   * if the directory already has a real owner, we *become* that owner. The operator keeps being
#     able to read their own backups and job log without sudo.
#   * if it is owned by root (a fresh named volume, or nothing there yet), we adopt it for the
#     built-in `printbridge` user.
#
# Either way the process that serves requests is not root.
set -e

DATA_DIR="${PRINTBRIDGE_DATA_DIR:-/data}"
mkdir -p "$DATA_DIR" "$DATA_DIR/spool"

if [ "$(id -u)" = "0" ]; then
    owner_uid="$(stat -c %u "$DATA_DIR" 2>/dev/null || echo 0)"
    owner_gid="$(stat -c %g "$DATA_DIR" 2>/dev/null || echo 0)"
    if [ "$owner_uid" = "0" ]; then
        owner_uid="$(id -u printbridge)"
        owner_gid="$(id -g printbridge)"
        chown -R "$owner_uid:$owner_gid" "$DATA_DIR" 2>/dev/null || true
    fi
    # Files left behind by an earlier version that ran as a different uid (keys.json is 0600, so
    # a mismatch is not a warning — it is a server that cannot read its own keys).
    find "$DATA_DIR" ! -user "$owner_uid" -exec chown "$owner_uid:$owner_gid" {} + 2>/dev/null || true
    # fontconfig and friends want a writable HOME; the adopted uid has no home directory here.
    HOME=/tmp
    export HOME
    exec setpriv --reuid "$owner_uid" --regid "$owner_gid" --clear-groups printbridge "$@"
fi

exec printbridge "$@"
