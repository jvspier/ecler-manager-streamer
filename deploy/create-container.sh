#!/usr/bin/env bash
# Create a Debian 13 LXC on Proxmox for the Ecler VEO Manager.
#
# Run this ON THE PROXMOX HOST as root, from the project directory:
#
#     ./deploy/create-container.sh                 # show what it would do, then ask
#     ./deploy/create-container.sh --deploy        # also push the code and install
#     ./deploy/create-container.sh --dry-run       # print the command and stop
#
# The container gets two NICs:
#   net0  your normal VLAN  - how you reach the dashboard, and how apt reaches
#                             the internet.  Carries the default gateway.
#   net1  the TV VLAN (52)  - how it reaches the Eclers.  Deliberately has NO
#                             gateway: a second default route would break
#                             routing, and this leg only needs its own subnet.
#
# Override any of these as environment variables, e.g.
#     MGMT_VLAN=10 TV_IP=10.0.2.250/24 ./deploy/create-container.sh

set -euo pipefail

# --- settings ---------------------------------------------------------
CTID=${CTID:-}                      # blank = next free ID
HOSTNAME_=${HOSTNAME_:-eclermanager}
BRIDGE=${BRIDGE:-vmbr0}
ROOTFS_STORAGE=${ROOTFS_STORAGE:-local-lvm}
TEMPLATE_STORAGE=${TEMPLATE_STORAGE:-local}
DISK_GB=${DISK_GB:-4}
MEMORY_MB=${MEMORY_MB:-512}
CORES=${CORES:-1}

MGMT_VLAN=${MGMT_VLAN:-}            # blank = untagged (native VLAN)
MGMT_IP=${MGMT_IP:-dhcp}            # dhcp, or CIDR like 10.0.5.20/24
MGMT_GW=${MGMT_GW:-}                # gateway, only used with a static MGMT_IP

TV_VLAN=${TV_VLAN:-20}
TV_IP=${TV_IP:-dhcp}                # dhcp, or CIDR like 10.0.2.250/24
                                    # no gateway here, on purpose

SSH_KEYS=${SSH_KEYS:-}              # optional path to an authorized_keys file

die() { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "→ $*"; }

DRY_RUN=false
DEPLOY=false
ASSUME_YES=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --deploy)  DEPLOY=true ;;
        -y|--yes)  ASSUME_YES=true ;;
        -h|--help) sed -n '2,30p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *) die "unknown argument: $arg" ;;
    esac
done

# --- pre-flight -------------------------------------------------------
[[ $EUID -eq 0 ]] || die "run as root on the Proxmox host"
command -v pct >/dev/null || die "pct not found - is this a Proxmox host?"
SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$SOURCE_DIR/run.py" ]] || die "run this from the project directory"

step "Pre-flight checks"

if [[ -z "$CTID" ]]; then
    CTID=$(pvesh get /cluster/nextid 2>/dev/null) || die "cannot get a free CTID"
    echo "  next free container ID: $CTID"
fi
[[ "$CTID" =~ ^[0-9]+$ ]] || die "CTID must be numeric, got '$CTID'"
if pct status "$CTID" &>/dev/null; then
    die "container $CTID already exists - set CTID=<other> to pick another"
fi

ip link show "$BRIDGE" &>/dev/null || die "bridge '$BRIDGE' not found (see: ip -br link)"
echo "  bridge $BRIDGE ✓"

pvesm status --storage "$ROOTFS_STORAGE" &>/dev/null \
    || die "storage '$ROOTFS_STORAGE' not found (see: pvesm status)"
echo "  rootfs storage $ROOTFS_STORAGE ✓"

step "Locating the Debian 13 template"
TEMPLATE_NAME=$(pveam list "$TEMPLATE_STORAGE" 2>/dev/null \
    | awk '/debian-13-standard/ {print $1}' | sort -V | tail -1)
if [[ -n "$TEMPLATE_NAME" ]]; then
    TEMPLATE="$TEMPLATE_NAME"
    echo "  already downloaded: ${TEMPLATE##*/} ✓"
else
    echo "  not present locally; looking it up …"
    pveam update >/dev/null 2>&1 || true
    AVAILABLE=$(pveam available --section system 2>/dev/null \
        | awk '/debian-13-standard/ {print $2}' | sort -V | tail -1)
    [[ -n "$AVAILABLE" ]] || die "no debian-13-standard template available (try: pveam available)"
    echo "  will download: $AVAILABLE"
    if [[ "$DRY_RUN" == false ]]; then
        pveam download "$TEMPLATE_STORAGE" "$AVAILABLE"
    fi
    TEMPLATE="$TEMPLATE_STORAGE:vztmpl/$AVAILABLE"
fi

# --- build the command ------------------------------------------------
net0="name=eth0,bridge=$BRIDGE"
if [[ -n "$MGMT_VLAN" ]]; then
    net0+=",tag=$MGMT_VLAN"
fi
if [[ "$MGMT_IP" == "dhcp" ]]; then
    net0+=",ip=dhcp"
else
    net0+=",ip=$MGMT_IP"
    if [[ -n "$MGMT_GW" ]]; then
        net0+=",gw=$MGMT_GW"
    else
        echo "  ! MGMT_IP is static but MGMT_GW is unset:"
        echo "    the container gets no default route, so apt will not work."
    fi
fi

net1="name=eth1,bridge=$BRIDGE,tag=$TV_VLAN"
if [[ "$TV_IP" == "dhcp" ]]; then
    net1+=",ip=dhcp"
else
    net1+=",ip=$TV_IP"
fi

CREATE_ARGS=(
    "$CTID" "$TEMPLATE"
    --hostname "$HOSTNAME_"
    --unprivileged 1
    --cores "$CORES"
    --memory "$MEMORY_MB"
    --swap 256
    --rootfs "$ROOTFS_STORAGE:$DISK_GB"
    --net0 "$net0"
    --net1 "$net1"
    --onboot 1
    --features nesting=0
    --description "Ecler VEO Manager - dashboard on :8477, TV VLAN $TV_VLAN on eth1"
)
if [[ -n "$SSH_KEYS" ]]; then
    CREATE_ARGS+=(--ssh-public-keys "$SSH_KEYS")
fi

cat <<EOF

──────────────────────────────────────────────────────────────────────
About to create container $CTID ("$HOSTNAME_"):

  pct create ${CREATE_ARGS[*]}

  eth0  your VLAN${MGMT_VLAN:+ (tag $MGMT_VLAN)}: $MGMT_IP${MGMT_GW:+ via $MGMT_GW}   <- dashboard + apt
  eth1  TV VLAN (tag $TV_VLAN): $TV_IP, no gateway   <- reaches the Eclers
──────────────────────────────────────────────────────────────────────
EOF

if [[ "$TV_IP" == "dhcp" ]]; then
    echo "! eth1 is set to DHCP. If VLAN $TV_VLAN has no DHCP server -- likely, since"
    echo "  your Eclers are statically addressed -- it will come up with no address."
    echo "  Set TV_IP to a free static address instead, e.g. TV_IP=10.0.2.250/24"
    echo
fi

if [[ "$DRY_RUN" == true ]]; then
    echo "(--dry-run: stopping here)"
    exit 0
fi

if [[ "$ASSUME_YES" == false ]]; then
    read -rp "Create it? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "aborted."; exit 1; }
fi

# --- create -----------------------------------------------------------
step "Creating container $CTID"
pct create "${CREATE_ARGS[@]}"
pct start "$CTID"
echo "  started; waiting for the network …"
for _ in $(seq 20); do
    pct exec "$CTID" -- test -d /proc/1 &>/dev/null && break
    sleep 1
done
sleep 3

step "Network as seen from inside the container"
pct exec "$CTID" -- ip -br addr show || true

if [[ "$DEPLOY" == false ]]; then
    cat <<EOF

Next: push the code in and install it.

  tar czf /tmp/eclermanager.tar.gz -C "$SOURCE_DIR" .
  pct exec $CTID -- mkdir -p /root/eclermanager
  pct push $CTID /tmp/eclermanager.tar.gz /root/eclermanager.tar.gz
  pct exec $CTID -- tar xzf /root/eclermanager.tar.gz -C /root/eclermanager
  pct exec $CTID -- bash /root/eclermanager/deploy/install.sh

Or re-run this script with --deploy to do all of that now.
EOF
    exit 0
fi

step "Pushing the project into the container"
tar czf /tmp/eclermanager.tar.gz -C "$SOURCE_DIR" .
pct exec "$CTID" -- mkdir -p /root/eclermanager
pct push "$CTID" /tmp/eclermanager.tar.gz /root/eclermanager.tar.gz
pct exec "$CTID" -- tar xzf /root/eclermanager.tar.gz -C /root/eclermanager
rm -f /tmp/eclermanager.tar.gz
echo "  pushed ✓"

step "Running the installer inside the container"
pct exec "$CTID" -- bash /root/eclermanager/deploy/install.sh

cat <<EOF

Container $CTID is up. To get a shell in it:  pct enter $CTID

Reachability check from inside (replace with one of your receiver IPs):

  pct exec $CTID -- python3 /opt/eclermanager/tools/probe.py 10.0.2.11

EOF
