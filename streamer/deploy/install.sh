#!/usr/bin/env bash
# Install the streamer on a Debian host. Run as root, from the checkout:
#
#     bash streamer/deploy/install.sh
#
# Safe to re-run. It never overwrites config.json or the credentials file:
# those are yours, and losing them would take four dashboards down.
set -euo pipefail

APP_DIR=/opt/eclerstreamer
CONF_DIR=/etc/eclerstreamer
STATE_DIR=/var/lib/eclerstreamer
USER=eclerstreamer

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "→ $*"; }

[[ $EUID -eq 0 ]] || die "run as root"
command -v python3 >/dev/null || die "python3 is not installed"

for tool in ffmpeg Xvfb; do
    command -v "$tool" >/dev/null || die "$tool is not installed:
  apt-get install -y ffmpeg xvfb chromium fonts-liberation fonts-dejavu-core"
done
command -v chromium >/dev/null || command -v chromium-browser >/dev/null \
    || die "no chromium found: apt-get install -y chromium"

# The web UI reaches systemd through sudo, so the package is a hard
# dependency -- and a minimal Debian does not have it. Checked here rather
# than at the sudoers step, which is most of the way through the install.
VISUDO="$(command -v visudo || true)"
[[ -z "$VISUDO" && -x /usr/sbin/visudo ]] && VISUDO=/usr/sbin/visudo
command -v sudo >/dev/null && [[ -n "$VISUDO" ]] \
    || die "sudo is not installed, and the web UI needs it to start and stop
  streams:  apt-get install -y sudo"

step "Service account"
if ! id -u "$USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR" --create-home \
            --shell /usr/sbin/nologin "$USER"
    echo "  created $USER"
else
    echo "  $USER exists ✓"
fi

step "Directories"
install -d -o "$USER" -g "$USER" -m 0755 "$STATE_DIR" "$STATE_DIR/run"
install -d -m 0755 "$CONF_DIR"

step "Application to $APP_DIR"
rm -rf "$APP_DIR"
install -d -m 0755 "$APP_DIR"
cp -r "$SRC/eclerstreamer" "$SRC/tools" "$SRC/stream.py" "$SRC/run.py" "$APP_DIR/"
chmod +x "$APP_DIR/stream.py" "$APP_DIR/run.py" "$APP_DIR/tools/pagesource.sh"
find "$APP_DIR" -name '__pycache__' -type d -prune -exec rm -rf {} +

step "Config"
if [[ -e "$CONF_DIR/config.json" ]]; then
    echo "  keeping existing config.json ✓"
else
    cat > "$CONF_DIR/config.json" <<'JSON'
{
  "local_addr": "",
  "interface": "",
  "manager_url": "",
  "qmin": 18,
  "no_bframes": true,
  "dashboards": []
}
JSON
    echo "  wrote a starter config.json"
    echo "  ! set local_addr to this host's address on the TV VLAN,"
    echo "    or multicast will leave by the default route and reach nothing."
fi
chown -R "$USER:$USER" "$CONF_DIR"
chmod 0640 "$CONF_DIR/config.json"

step "systemd units"
install -m 0644 "$SRC/deploy/dashboard-stream@.service" /etc/systemd/system/
install -m 0644 "$SRC/deploy/eclerstreamer.service" /etc/systemd/system/

step "sudo rule for the five stream verbs"
# Validate before installing: a broken sudoers file locks everyone out of sudo,
# so it is checked in place and only moved if visudo accepts it.
tmp_rule="$(mktemp)"
cp "$SRC/deploy/eclerstreamer.sudoers" "$tmp_rule"
if "$VISUDO" -cqf "$tmp_rule"; then
    install -m 0440 -o root -g root "$tmp_rule" /etc/sudoers.d/eclerstreamer
    echo "  installed ✓"
else
    rm -f "$tmp_rule"
    die "the sudoers rule did not validate; nothing was installed"
fi
rm -f "$tmp_rule"

systemctl daemon-reload
systemctl enable eclerstreamer.service
# restart, not "enable --now": that starts a stopped unit but leaves a running
# one alone, so re-running the installer would quietly keep serving the old
# code and the old sandbox -- which looks exactly like the fix not working.
systemctl restart eclerstreamer.service

echo
echo "✓ installed."
echo
echo "  Web UI:   http://$(hostname -I | awk '{print $1}'):8478/"
echo "  Config:   $CONF_DIR/config.json"
echo "  Logs:     journalctl -u eclerstreamer -f"
echo
echo "  Set a login before putting it on a shared network:"
echo "    python3 $SRC/tools/setpassword.py --env $CONF_DIR/eclerstreamer.env --user admin"
echo
echo "  Each dashboard is a unit:  systemctl enable --now dashboard-stream@5"
