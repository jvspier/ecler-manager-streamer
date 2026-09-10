#!/usr/bin/env bash
# Render a web page on a virtual display, so ffmpeg can capture it.
#
# The other half of the software-streaming experiment: tools/teststream.py
# --url captures an X display, and this is what puts a page on one.
#
#     bash tools/pagesource.sh --url https://example.com/dashboard
#     python3 tools/teststream.py --channel 5 --interface eth1 --url
#
# Nothing is displayed on a physical output: Xvfb is a framebuffer in memory.
# The browser renders into it and ffmpeg reads it with x11grab.
#
#   --url <address>     the page to show (required)
#   --display :N        X display to use (default :99)
#   --size WxH          framebuffer size (default 1920x1080)
#   --browser <path>    override browser autodetection
#   --stop              kill whatever this script started on --display
#   --status            report what is running on --display
#
# Dependencies, on Debian/Ubuntu:
#
#     apt-get install -y xvfb chromium
#
# Leave it running; it holds the display open until stopped. Everything it
# starts is recorded in /run/pagesource-<display>.pids so --stop is reliable.

set -euo pipefail

URL=""
DISPLAY_NUM=":99"
SIZE="1920x1080"
BROWSER=""
ACTION="start"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --url)     URL=${2:?--url needs an address}; shift 2 ;;
        --display) DISPLAY_NUM=${2:?--display needs e.g. :99}; shift 2 ;;
        --size)    SIZE=${2:?--size needs e.g. 1920x1080}; shift 2 ;;
        --browser) BROWSER=${2:?--browser needs a path}; shift 2 ;;
        --stop)    ACTION="stop"; shift ;;
        --status)  ACTION="status"; shift ;;
        -h|--help) sed -n '2,28p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *)         echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

die() { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "→ $*"; }

PIDFILE="/run/pagesource-${DISPLAY_NUM#:}.pids"
# The browser's own output is the only useful diagnostic when a page renders
# black, so it is kept rather than discarded.
LOGFILE="/tmp/pagesource-${DISPLAY_NUM#:}.log"

case "$ACTION" in
status)
    if [[ -f "$PIDFILE" ]]; then
        echo "recorded for $DISPLAY_NUM:"
        while read -r pid what; do
            if kill -0 "$pid" 2>/dev/null; then
                echo "  $pid  $what  (running)"
            else
                echo "  $pid  $what  (GONE -- this is why the display is black)"
            fi
        done < "$PIDFILE"
    else
        echo "nothing recorded for $DISPLAY_NUM"
    fi
    if command -v xwininfo >/dev/null; then
        echo
        echo "windows on $DISPLAY_NUM:"
        if DISPLAY="$DISPLAY_NUM" xwininfo -root -children 2>/dev/null \
                | sed -n "/children:/,\$p" | sed "s/^/  /" | head -12; then :; fi
        echo "  (no child windows means nothing is drawing)"
    else
        echo
        echo "install x11-utils for a window list: apt-get install -y x11-utils"
    fi
    if [[ -f "$LOGFILE" ]]; then
        echo
        echo "last of $LOGFILE:"
        tail -15 "$LOGFILE" | sed "s/^/  /"
    fi
    exit 0
    ;;
stop)
    [[ -f "$PIDFILE" ]] || { echo "nothing to stop for $DISPLAY_NUM"; exit 0; }
    while read -r pid what; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null && echo "  stopped $what ($pid)"
        fi
    done < "$PIDFILE"
    sleep 1
    # Anything that ignored SIGTERM.
    while read -r pid what; do
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null \
            && echo "  forced $what ($pid)"
    done < "$PIDFILE"
    rm -f "$PIDFILE"
    exit 0
    ;;
esac

[[ -n "$URL" ]] || die "--url is required"
[[ "$URL" =~ ^https?:// ]] || die "--url must start with http:// or https://"
[[ "$SIZE" =~ ^[0-9]+x[0-9]+$ ]] || die "--size must look like 1920x1080"
[[ "$DISPLAY_NUM" =~ ^:[0-9]+$ ]] || die "--display must look like :99"

command -v Xvfb >/dev/null || die "Xvfb is not installed:
  apt-get install -y xvfb"

if [[ -z "$BROWSER" ]]; then
    for candidate in chromium chromium-browser google-chrome google-chrome-stable \
                     firefox; do
        if command -v "$candidate" >/dev/null; then BROWSER=$candidate; break; fi
    done
fi
[[ -n "$BROWSER" ]] || die "no browser found:
  apt-get install -y chromium
  (or pass --browser /path/to/one)"

if [[ -f "$PIDFILE" ]]; then
    die "$DISPLAY_NUM already has something recorded in $PIDFILE.
  Stop it first:  bash $0 --display $DISPLAY_NUM --stop"
fi

: > "$PIDFILE"
cleanup_on_failure() {
    # Do not leave half a stack running if the browser fails to come up.
    while read -r pid _; do kill "$pid" 2>/dev/null || true; done < "$PIDFILE"
    rm -f "$PIDFILE"
}

step "Starting Xvfb on $DISPLAY_NUM at $SIZE"
Xvfb "$DISPLAY_NUM" -screen 0 "${SIZE}x24" -nolisten tcp &
XVFB_PID=$!
echo "$XVFB_PID Xvfb" >> "$PIDFILE"
sleep 2
kill -0 "$XVFB_PID" 2>/dev/null || { cleanup_on_failure; die "Xvfb exited"; }
echo "  pid $XVFB_PID"

WIDTH=${SIZE%x*}
HEIGHT=${SIZE#*x}

step "Starting $BROWSER on $DISPLAY_NUM"
echo "  $URL"
# --kiosk removes all chrome; the rest keeps a container-friendly browser quiet
# and stops it drawing anything over the page.
DISPLAY="$DISPLAY_NUM" "$BROWSER" \
    --kiosk \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --noerrdialogs \
    --no-first-run \
    --window-size="$WIDTH,$HEIGHT" \
    --window-position=0,0 \
    --user-data-dir="/tmp/pagesource-${DISPLAY_NUM#:}" \
    "$URL" >>"$LOGFILE" 2>&1 &
BROWSER_PID=$!
echo "$BROWSER_PID $BROWSER" >> "$PIDFILE"
echo "  pid $BROWSER_PID, log $LOGFILE"

# A browser can survive its first seconds and still never draw, so wait for a
# window rather than for the clock.
step "Waiting for it to draw"
drew=false
for attempt in $(seq 1 20); do
    if ! kill -0 "$BROWSER_PID" 2>/dev/null; then
        echo
        echo "  the browser exited. Its output:"
        tail -20 "$LOGFILE" 2>/dev/null | sed 's/^/    /'
        cleanup_on_failure
        die "$BROWSER did not stay running. Try it in the foreground:
    DISPLAY=$DISPLAY_NUM $BROWSER --kiosk --no-sandbox '$URL'"
    fi
    if command -v xwininfo >/dev/null; then
        if DISPLAY="$DISPLAY_NUM" xwininfo -root -children 2>/dev/null \
                | grep -qE '^ +0x[0-9a-f]+'; then
            drew=true
            echo "  a window appeared after ${attempt}s"
            break
        fi
    else
        sleep 5
        drew=true            # cannot check; assume and let the screenshot tell
        echo "  install x11-utils to detect this properly; waited 5s"
        break
    fi
    sleep 1
done
if [[ "$drew" == false ]]; then
    echo
    echo "  ! no window after 20s. The browser is running but not drawing."
    echo "    Its output:"
    tail -20 "$LOGFILE" 2>/dev/null | sed 's/^/      /'
    echo "    Leaving it up so you can look; stop it with --stop."
fi

cat <<EOF

✓ $DISPLAY_NUM is showing the page. Capture it with:

    python3 tools/teststream.py --channel <N> --interface <iface> \\
        --url --display $DISPLAY_NUM

Check what it actually rendered, before streaming it anywhere:

    apt-get install -y x11-apps imagemagick
    DISPLAY=$DISPLAY_NUM import -window root /tmp/page.png

Stop everything:

    bash $0 --display $DISPLAY_NUM --stop
EOF
