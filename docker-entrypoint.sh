#!/bin/bash

set -euo pipefail

runtime_uid="$(id -u pillbug)"
runtime_gid="$(id -g pillbug)"

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

if [[ "$(id -u)" == "0" ]]; then
    ensure_dir_ownership "/var/lib/pillbug"
    ensure_dir_ownership "/var/lib/pillbug-dashboard"
    ensure_dir_ownership "${PB_BASE_DIR:-}"
    ensure_dir_ownership "${PB_DASHBOARD_BASE_DIR:-}"

    exec su -s /bin/bash pillbug -c 'exec "$@"' -- "$@"
fi

exec "$@"
