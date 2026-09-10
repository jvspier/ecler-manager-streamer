#!/usr/bin/env bash
# Render a web page on a virtual display, so ffmpeg can capture it.
#
# The other half of the software-streaming experiment: tools/teststream.py
# --url captures an X display, and this is what puts a page on one.
#
#     bash tools/pagesource.sh --url https://example.com/dashboard
#     python3 tools/teststream.py --channel 5 --interface eth1 --from-display :99
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
#   --screenshot <path> grab the display through ffmpeg x11grab, the same way
#                       streaming does, and say if it looks blank
#   --kiosk             use --kiosk instead of a self-mapped window. Needs a
#                       window manager on the display, or nothing is drawn
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
USE_KIOSK=false
SHOT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --url)     URL=${2:?--url needs an address}; shift 2 ;;
        --display) DISPLAY_NUM=${2:?--display needs e.g. :99}; shift 2 ;;
        --size)    SIZE=${2:?--size needs e.g. 1920x1080}; shift 2 ;;
        --browser) BROWSER=${2:?--browser needs a path}; shift 2 ;;
        --stop)    ACTION="stop"; shift ;;
        --status)  ACTION="status"; shift ;;
        --kiosk)   USE_KIOSK=true; shift ;;
        --screenshot) SHOT=${2:?--screenshot needs a path}; ACTION="shot"; shift 2 ;;
        -h|--help) sed -n '2,28p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *)         echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

die() { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "→ $*"; }

PIDFILE="/run/pagesource-${DISPLAY_NUM#:}.pids"
if [[ "$USE_KIOSK" == true ]]; then
    KIOSK_FLAGS="--kiosk"
    APP_FLAG=""            # the URL is passed positionally in kiosk mode
else
    KIOSK_FLAGS=""
    APP_FLAG=""            # filled in once URL is validated, below
fi
# The browser's own output is the only useful diagnostic when a page renders
# black, so it is kept rather than discarded.
LOGFILE="/tmp/pagesource-${DISPLAY_NUM#:}.log"
XVFB_LOG="/tmp/pagesource-${DISPLAY_NUM#:}-xvfb.log"

case "$ACTION" in
shot)
    # Grab through ffmpeg's x11grab -- the same path teststream.py uses -- so
    # this answers the question that matters, rather than what a different
    # screenshot tool makes of the display.
    command -v ffmpeg >/dev/null || die "ffmpeg is needed for --screenshot:
  apt-get install -y ffmpeg"
    echo "→ grabbing $DISPLAY_NUM with ffmpeg x11grab (the streaming path)"
    # -draw_mouse 0: leave the X pointer out of the frame. See the Xvfb
    # comment below for why this is done here and not on the display.
    if ffmpeg -hide_banner -loglevel error -f x11grab -draw_mouse 0 \
            -video_size "$SIZE" \
            -i "$DISPLAY_NUM" -frames:v 1 -y "$SHOT" 2>&1; then
        BYTES=$(stat -c%s "$SHOT" 2>/dev/null || echo 0)
        echo "  wrote $SHOT ($BYTES bytes)"
        if [[ "$BYTES" -lt 5000 ]]; then
            echo
            echo "  ! that is small enough to be a blank screen. A 1920x1080"
            echo "    PNG of anything real is tens of KB or more."
            if command -v xwininfo >/dev/null; then
                echo
                echo "    windows on $DISPLAY_NUM:"
                DISPLAY="$DISPLAY_NUM" xwininfo -root -children 2>/dev/null \
                    | sed -n '/children:/,$p' | sed 's/^/      /' | head -10
                echo "    A window that is not IsViewable, or is 1x1, is never"
                echo "    composited to the root window -- which is what a"
                echo "    capture reads."
            fi
        fi
        exit 0
    fi
    die "ffmpeg could not read $DISPLAY_NUM. Is Xvfb running on it?
  bash $0 --display $DISPLAY_NUM --status"
    ;;
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
            # setsid makes each child a process-group leader, so signal the
            # whole group: a browser leaves helper processes behind otherwise.
            kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null
            echo "  stopped $what ($pid)"
        fi
    done < "$PIDFILE"
    sleep 1
    # Anything that ignored SIGTERM.
    while read -r pid what; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 -- "-$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null
            echo "  forced $what ($pid)"
        fi
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

# A browser needs real memory. Being killed for the lack of it looks like a
# mysterious crash -- a window appears, then the process is gone -- so check
# before starting rather than after.
AVAILABLE=$(awk '/MemAvailable/ {print int($2 / 1024)}' /proc/meminfo 2>/dev/null || echo 0)
if [[ "$AVAILABLE" -gt 0 && "$AVAILABLE" -lt 900 ]]; then
    die "only ${AVAILABLE} MB of memory is available.
  A browser needs roughly 300-800 MB and will be killed part-way through
  starting, which shows up as a window that appears and then vanishes.
  In a Proxmox container:  pct set <ctid> -memory 4096"
fi
if [[ "$AVAILABLE" -gt 0 && "$AVAILABLE" -lt 1500 ]]; then
    echo "! only ${AVAILABLE} MB available; a browser may be tight" >&2
fi

# A crashed Chromium leaves a SingletonLock and a "was not shut down
# correctly" flag in its profile, and the next start can refuse or sit on a
# restore prompt instead of the page.  The profile holds nothing worth keeping
# for a kiosk browser, so start clean every time.
PROFILE="/tmp/pagesource-${DISPLAY_NUM#:}-profile"
rm -rf "$PROFILE"
mkdir -p "$PROFILE"

step "Starting Xvfb on $DISPLAY_NUM at $SIZE"
# setsid plus closed inherited descriptors: without both, this script's caller
# waits for these children even after the script itself has finished -- which
# through `pct exec` means a terminal that never comes back.
# The X pointer sits in the middle of the framebuffer and would be captured
# into the stream, but Xvfb -nocursor does NOT remove it: that flag only
# suppresses the server's default root cursor, and Chromium sets a cursor on
# its own window, which still draws. Tried and measured -- the arrow was still
# there mid-frame. It is excluded at the capture instead, with ffmpeg's
# -draw_mouse 0, in both this script's --screenshot and teststream.py.
setsid Xvfb "$DISPLAY_NUM" -screen 0 "${SIZE}x24" -nolisten tcp \
    >>"$XVFB_LOG" 2>&1 </dev/null &
XVFB_PID=$!
echo "$XVFB_PID Xvfb" >> "$PIDFILE"
sleep 2
kill -0 "$XVFB_PID" 2>/dev/null || { cleanup_on_failure; die "Xvfb exited"; }
echo "  pid $XVFB_PID"

WIDTH=${SIZE%x*}
HEIGHT=${SIZE#*x}

if [[ "$USE_KIOSK" == true ]]; then
    APP_FLAG="$URL"
else
    # --app opens the page as its own chromeless window, which the browser maps
    # itself: no window manager needed, unlike --kiosk.
    APP_FLAG="--app=$URL"
fi

step "Starting $BROWSER on $DISPLAY_NUM"
echo "  $URL"
# Two deliberate choices here, each learned the hard way:
#
# --no-zygote. Chromium normally pre-forks a "zygote" process and clones render
# processes from it using CLONE_NEWUSER/NEWPID/NEWNET. An unprivileged LXC
# blocks that even with nesting=1 and --no-sandbox, and the browser dies within
# a second or two of drawing its first window, logging "Failed to send
# GetTerminationStatus message to zygote" amid a lot of unrelated dbus noise.
# --no-zygote skips that model; process spawning is marginally slower, which
# does not matter for a browser showing one page.
#
# NOT --kiosk. Kiosk mode asks a window manager to make the window fullscreen,
# and a bare Xvfb has no window manager, so the request goes nowhere. --app
# opens the page as its own chromeless window which the browser maps itself,
# sized by --window-size. Pass --kiosk if the display does have a WM.
# Exactly the flag set proven to work by hand, and nothing more. Every extra
# flag added here was a guess, and one of them was stopping the browser from
# staying up; a kiosk browser needs none of them. Add back only what a real
# problem demands.
#
#   --no-zygote          the container blocks Chromium's zygote process model
#   --no-sandbox         an unprivileged container cannot use the sandbox
#   --disable-gpu        there is no GPU on the virtual display
#   --disable-dev-shm-usage  /dev/shm is small in a container
#   --app=<url>          a chromeless window the browser maps itself, so no
#                        window manager is needed (unlike --kiosk)
#   --test-type          suppresses the yellow "you are using an unsupported
#                        command-line flag: --no-sandbox" infobar, which
#                        otherwise steals ~70px off the top of every frame.
#                        Cosmetic only -- if the browser ever stops staying
#                        up, drop this one first.
#
# setsid on the browser too, and this is the crux of it. Closing the inherited
# descriptors stops the caller waiting, but the browser stays in the caller's
# process group -- so when that session ends, whether by Ctrl-C or simply by
# the `pct exec` finishing, the signal reaches the browser and it dies.
#
# The symptom was thoroughly misleading: "a window appeared after 2s", the
# script reporting success, and a display that was black by the time anyone
# looked, with a log full of dbus noise and no cause in it. The browser had
# never crashed at all. setsid puts it in its own session, where the terminal
# cannot reach it.
DISPLAY="$DISPLAY_NUM" setsid "$BROWSER" \
    --no-sandbox \
    --no-zygote \
    --disable-gpu \
    --disable-dev-shm-usage \
    --test-type \
    --window-size="$WIDTH,$HEIGHT" \
    --user-data-dir="$PROFILE" \
    $KIOSK_FLAGS \
    $APP_FLAG \
    >>"$LOGFILE" 2>&1 </dev/null &
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
        echo "  the browser exited after ${attempt}s. Its output:"
        tail -20 "$LOGFILE" 2>/dev/null | sed 's/^/    /'
        echo
        echo "  Reading that log: 'Failed to connect to the bus' and"
        echo "  'NameHasOwner' are dbus noise and harmless. What matters is"
        echo "  anything about the zygote, or nothing at all -- both usually"
        echo "  mean it was killed for memory. Check:"
        echo "      free -m"
        echo "      dmesg -T | grep -i 'oom\\|killed process'"
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

    python3 tools/teststream.py --channel <N> --local-addr <ip-on-tv-vlan> \\
        --from-display $DISPLAY_NUM

(--from-display reuses this display. teststream.py --url ADDRESS starts its
own browser instead, which you do not want while this one is up.)

Check what it actually rendered, before streaming it anywhere -- this needs
no extra packages, it grabs through the same ffmpeg path as the stream:

    bash $0 --display $DISPLAY_NUM --screenshot /tmp/page.png

Stop everything:

    bash $0 --display $DISPLAY_NUM --stop
EOF
