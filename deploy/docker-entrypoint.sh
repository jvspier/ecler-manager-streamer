#!/bin/sh
# Seed a config on first run, then hand over to the application.
#
# Without this an empty volume means "No config at ...", exit 2, and a
# container that restarts for ever with a message nobody sees. Seeding lets
# the UI come up so the file can be edited -- it is in your mounted volume.
set -e

CONFIG=/etc/eclermanager/config.json
for arg in "$@"; do
    case "$prev" in --config) CONFIG=$arg ;; esac
    prev=$arg
done

if [ ! -f "$CONFIG" ]; then
    echo "→ no config at $CONFIG - seeding from config.example.json"
    cp /opt/eclermanager/config.example.json "$CONFIG"
    echo "  Edit it on the host (it is in your mounted volume), then restart"
    echo "  this container. Or use 'Scan network' in the UI to find devices."
fi

exec python3 /opt/eclermanager/run.py "$@"
