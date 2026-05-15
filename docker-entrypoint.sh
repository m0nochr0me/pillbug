#!/bin/bash

set -euo pipefail

PILLBUG_USER="${PILLBUG_USER:-pillbug}"
PILLBUG_GROUP="${PILLBUG_GROUP:-pillbug}"

if [[ "$(id -u)" == "0" ]]; then
    current_uid="$(id -u "$PILLBUG_USER")"
    current_gid="$(id -g "$PILLBUG_USER")"

    target_uid="${PUID:-$current_uid}"
    target_gid="${PGID:-$current_gid}"

    if [[ "$target_gid" != "$current_gid" ]]; then
        groupmod --non-unique --gid "$target_gid" "$PILLBUG_GROUP"
    fi
    if [[ "$target_uid" != "$current_uid" ]]; then
        usermod --non-unique --uid "$target_uid" "$PILLBUG_USER"
    fi

    runtime_uid="$(id -u "$PILLBUG_USER")"
    runtime_gid="$(id -g "$PILLBUG_USER")"

    ensure_dir_ownership() {
        local dir_path="$1"

        if [[ -z "$dir_path" || ! -d "$dir_path" ]]; then
            return
        fi

        local current_owner
        current_owner="$(stat -c '%u:%g' "$dir_path")"
        if [[ "$current_owner" == "${runtime_uid}:${runtime_gid}" ]]; then
            return
        fi

        chown -R "${runtime_uid}:${runtime_gid}" "$dir_path"
    }

    ensure_dir_ownership "/home/${PILLBUG_USER}"
    ensure_dir_ownership "${PB_BASE_DIR:-}"
    ensure_dir_ownership "${PB_DASHBOARD_BASE_DIR:-}"

    exec setpriv \
        --reuid="$runtime_uid" \
        --regid="$runtime_gid" \
        --init-groups \
        --no-new-privs \
        "$@"
fi

exec "$@"
