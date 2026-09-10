#!/usr/bin/env bash
# Install the Ecler VEO Manager as a systemd service.
#
# Run this INSIDE the container (or on any always-on Debian/Ubuntu box that can
# reach the VEO VLAN), from the project directory, as root:
#
#     cd /root/eclermanager && ./deploy/install.sh
#
# Pass --config <file> to install that config too, replacing whatever is in
# place.  Doing it here rather than afterwards matters: the service reads its
# config once at startup, so a config copied in after the restart would not
# take effect until the next one.
#
# Idempotent: safe to re-run to upgrade the code in place.  Without --config,
# your config at /etc/eclermanager/config.json is never touched.

set -euo pipefail

INSTALL_CONFIG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)
            INSTALL_CONFIG=${2:?--config needs a path}
            shift 2
            ;;
        -h|--help)
            sed -n '2,14p' "$0" | sed 's/^#\{1,\} \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

APP_DIR=${APP_DIR:-/opt/eclermanager}
CONFIG_DIR=${CONFIG_DIR:-/etc/eclermanager}
STATE_DIR=${STATE_DIR:-/var/lib/eclermanager}
SERVICE_USER=${SERVICE_USER:-eclermanager}
SERVICE_NAME=eclermanager

die() { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "→ $*"; }

[[ $EUID -eq 0 ]] || die "run as root"
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$SOURCE_DIR/run.py" ]] || die "cannot find run.py next to this script"

step "Checking Python"
command -v python3 >/dev/null || {
    echo "  installing python3 …"
    apt-get update -qq
    apt-get install -y --no-install-recommends python3
}
PYTHON_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 - <<'PY' || die "Python 3.11+ required, found $PYTHON_VERSION"
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY
echo "  python3 $PYTHON_VERSION ✓"

step "Creating service user '$SERVICE_USER'"
if id "$SERVICE_USER" &>/dev/null; then
    echo "  already exists ✓"
else
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "  created ✓"
fi

step "Installing application to $APP_DIR"
mkdir -p "$APP_DIR"
if [[ "$SOURCE_DIR" == "$APP_DIR" ]]; then
    # Re-run from the installed copy: nothing to copy onto itself.
    echo "  already running from $APP_DIR, skipping copy"
else
    # Copy code only; never the caller's config.json or event log.
    for item in run.py README.md LICENSE config.example.json \
                devices.example.txt devices.txt docs \
                eclermanager tools tests deploy; do
        if [[ -e "$SOURCE_DIR/$item" ]]; then
            cp -r "$SOURCE_DIR/$item" "$APP_DIR/"
        fi
    done
fi
find "$APP_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"
echo "  installed ✓ (read-only to the service)"

step "Preparing $CONFIG_DIR and $STATE_DIR"
mkdir -p "$CONFIG_DIR" "$STATE_DIR"
FRESH_CONFIG=false
if [[ -n "$INSTALL_CONFIG" ]]; then
    [[ -f "$INSTALL_CONFIG" ]] || die "no such config: $INSTALL_CONFIG"
    # Refuse to install a config the app cannot load, rather than leaving the
    # service crash-looping on it.
    if ! python3 -c "
import sys
sys.path.insert(0, '$APP_DIR')
from eclermanager.config import load
cfg = load('$INSTALL_CONFIG')
print(f'  validated: {len(cfg.active_receivers)} active receiver(s), '
      f'{len(cfg.channels)} channel(s)')
"; then
        die "$INSTALL_CONFIG is not a valid config; nothing was changed"
    fi
    if [[ -f "$CONFIG_DIR/config.json" ]]; then
        if cmp -s "$INSTALL_CONFIG" "$CONFIG_DIR/config.json"; then
            echo "  config unchanged"
        else
            # The live config holds renames and "should be" edits made from the
            # dashboard.  Never replace it without leaving a way back.
            BACKUP="$CONFIG_DIR/config.json.$(date +%Y%m%d-%H%M%S).bak"
            cp -p "$CONFIG_DIR/config.json" "$BACKUP"
            chmod 640 "$BACKUP"
            cp "$INSTALL_CONFIG" "$CONFIG_DIR/config.json"
            echo "  replaced config; previous one saved as"
            echo "    $BACKUP"
            echo "  ! any renames made in the dashboard were in that file"
        fi
    else
        cp "$INSTALL_CONFIG" "$CONFIG_DIR/config.json"
        echo "  installed config from $INSTALL_CONFIG"
    fi
elif [[ -f "$CONFIG_DIR/config.json" ]]; then
    echo "  keeping existing config.json ✓"
else
    cp "$SOURCE_DIR/config.example.json" "$CONFIG_DIR/config.json"
    FRESH_CONFIG=true
    echo "  seeded config.json from the example (needs your IPs)"
fi
# The service rewrites config.json from the dashboard, so it must own it.
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONFIG_DIR" "$STATE_DIR"
chmod 750 "$CONFIG_DIR" "$STATE_DIR"
chmod 640 "$CONFIG_DIR/config.json"

step "Checking the dashboard login"
ENV_FILE="$CONFIG_DIR/eclermanager.env"
if [[ -f "$ENV_FILE" ]]; then
    chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
    chmod 640 "$ENV_FILE"
    if grep -q '^ECLER_AUTH_USER=..*' "$ENV_FILE"; then
        echo "  login configured ✓ ($(grep '^ECLER_AUTH_USER=' "$ENV_FILE" | cut -d= -f2))"
    else
        echo "  ! $ENV_FILE exists but sets no account; the dashboard is open"
    fi
else
    echo "  no login configured - the dashboard is open to anyone who can"
    echo "    reach the port. Set one up with:"
    echo "      python3 $APP_DIR/tools/setpassword.py --user <name> \\"
    echo "          --env-file $ENV_FILE --owner $SERVICE_USER"
    echo "      systemctl restart $SERVICE_NAME"
fi

step "Installing systemd unit"
install -m 644 "$SOURCE_DIR/deploy/eclermanager.service" \
    /etc/systemd/system/"$SERVICE_NAME".service
# Only refresh the extra-IP unit if it is already in use; installing it here
# would otherwise add an address nobody asked for.
if systemctl list-unit-files eclermanager-extra-ip.service &>/dev/null \
        && [[ -f /etc/systemd/system/eclermanager-extra-ip.service ]]; then
    install -m 644 "$SOURCE_DIR/deploy/eclermanager-extra-ip.service" \
        /etc/systemd/system/eclermanager-extra-ip.service
    echo "  refreshed eclermanager-extra-ip.service ✓"
fi
systemctl daemon-reload
echo "  installed ✓"

if [[ "$FRESH_CONFIG" == true ]]; then
    # Use the shipped inventory in the hint when there is one.
    if [[ -f "$APP_DIR/devices.txt" ]]; then
        NAMES_FLAG="--names devices.txt "
        RANGES=$(awk '{print $1}' "$APP_DIR/devices.txt" 2>/dev/null \
            | grep -Eo '^[0-9]+\.[0-9]+\.[0-9]+\.' | sort -u \
            | sed 's/$/1-254/' | tr '\n' ' ')
        [[ -z "$RANGES" ]] && RANGES="<your-subnet>/24"
    else
        NAMES_FLAG=""
        RANGES="<your-subnet>/24"
    fi
    cat <<EOF

──────────────────────────────────────────────────────────────────────
Installed, but NOT started: $CONFIG_DIR/config.json is still the example.

  1. Discover your devices (read-only, changes nothing):

       cd $APP_DIR
       python3 tools/scan.py $RANGES ${NAMES_FLAG}\\
           --transmitter <tx-ip> [--transmitter <tx-ip> ...] \\
           --write-config /tmp/discovered.json

  2. Review it, then put it in place:

       \$EDITOR /tmp/discovered.json
       install -o $SERVICE_USER -g $SERVICE_USER -m 640 \\
           /tmp/discovered.json $CONFIG_DIR/config.json

  3. Start it:

       systemctl enable --now $SERVICE_NAME
       systemctl status $SERVICE_NAME
──────────────────────────────────────────────────────────────────────
EOF
else
    step "Restarting $SERVICE_NAME"
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl restart "$SERVICE_NAME"
    sleep 2
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        ADDRESSES=$(hostname -I 2>/dev/null || echo "<container-ip>")
        echo "  running ✓"
        echo
        for address in $ADDRESSES; do echo "  Dashboard: http://$address:8477/"; done
    else
        echo "  ✗ failed to start. Logs:"
        journalctl -u "$SERVICE_NAME" -n 30 --no-pager
        exit 1
    fi
fi
