#!/bin/sh
set -eu

export MUNICH_DATA_DIR="${MUNICH_DATA_DIR:-${RAILWAY_VOLUME_MOUNT_PATH:-/app/data}}"
if [ "$(id -u)" -eq 0 ]; then
    # Railway mounts the volume as root. Keep the worker and Chrome unprivileged.
    install -d -m 0700 -o mietradar -g mietradar "$MUNICH_DATA_DIR"
    for database in "$MUNICH_DATA_DIR"/munich_radar.sqlite3 "$MUNICH_DATA_DIR"/munich_radar.sqlite3-*; do
        if [ -f "$database" ] && [ ! -L "$database" ]; then
            chown mietradar:mietradar "$database"
            chmod 0600 "$database"
        fi
    done
    export HOME=/home/mietradar
    exec gosu mietradar "$@"
fi
exec "$@"
