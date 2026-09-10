#!/usr/bin/env bash
# Update from git and reinstall.  Run INSIDE the container:
#
#     bash /root/eclermanager/deploy/update.sh
#
# Or from the Proxmox host in one line:
#
#     pct exec <ctid> -- bash /root/eclermanager/deploy/update.sh
#
# Pulls the checkout, shows what changed, runs the tests, and only then
# reinstalls and restarts.  Your config and login are never touched: see
# install.sh.
#
#   --branch <name>   pull a different branch (default: the current one)
#   --no-tests        skip the test run (not recommended)
#   --dry-run         fetch and show what would change, then stop

set -euo pipefail

CHECKOUT=${CHECKOUT:-/root/eclermanager}
BRANCH=""
RUN_TESTS=true
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --branch)   BRANCH=${2:?--branch needs a name}; shift 2 ;;
        --no-tests) RUN_TESTS=false; shift ;;
        --dry-run)  DRY_RUN=true; shift ;;
        -h|--help)  sed -n '2,16p' "$0" | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
        *)          echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

die() { echo "✗ $*" >&2; exit 1; }
step() { echo; echo "→ $*"; }

[[ $EUID -eq 0 ]] || die "run as root"
[[ -d "$CHECKOUT/.git" ]] || die "$CHECKOUT is not a git checkout. See the
  one-time setup in docs/deployment.md, or set CHECKOUT=<path>."
command -v git >/dev/null || die "git is not installed: apt-get install -y git"

cd "$CHECKOUT"
[[ -n "$BRANCH" ]] || BRANCH=$(git rev-parse --abbrev-ref HEAD)

step "Fetching origin"
git fetch --quiet origin "$BRANCH" || die "could not fetch $(git remote get-url origin).
  A public repo over HTTPS needs no credential; a private one does.
  Over SSH:   ssh -T git@github.com     (expect \"successfully authenticated\")
  Over HTTPS: the stored token may have expired.
  Either way the container only reads -- nothing is ever pushed from here."

BEFORE=$(git rev-parse HEAD)
AFTER=$(git rev-parse "origin/$BRANCH")

if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "  already up to date at ${BEFORE:0:8}"
    if [[ "$DRY_RUN" == true ]]; then exit 0; fi
    step "Reinstalling anyway, in case /opt has drifted"
else
    echo "  ${BEFORE:0:8} -> ${AFTER:0:8}"
    echo
    git --no-pager log --oneline "$BEFORE..$AFTER" | sed 's/^/    /'
    echo
    git --no-pager diff --stat "$BEFORE..$AFTER" | tail -15 | sed 's/^/    /'
fi

if [[ "$DRY_RUN" == true ]]; then
    echo
    echo "(--dry-run: nothing was changed)"
    exit 0
fi

# Refuse to clobber local edits rather than losing them silently.
if ! git diff --quiet || ! git diff --cached --quiet; then
    die "there are uncommitted changes in $CHECKOUT.
  Commit, stash or discard them first:  git -C $CHECKOUT status"
fi

step "Updating the checkout"
git merge --ff-only "origin/$BRANCH" || die "cannot fast-forward. The local
  branch has diverged; sort it out by hand in $CHECKOUT."
echo "  now at $(git rev-parse --short HEAD)"

if [[ "$RUN_TESTS" == true ]]; then
    step "Running the tests before touching the running service"
    if python3 -m unittest discover -s tests -q 2>&1 | tail -3; then
        echo "  passed ✓"
    else
        die "tests failed on the new code. The running service is untouched.
  Investigate, or re-run with --no-tests to install anyway."
    fi
fi

step "Installing"
bash "$CHECKOUT/deploy/install.sh"
