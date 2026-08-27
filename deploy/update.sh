#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")
TARGET=${1:-origin/main}
RELEASE_REPOSITORY=${OUTPOST_RELEASE_REPOSITORY:-baal-bot/Outpost-Meshtastic}
RELEASE_TEMP=

cleanup() {
  if [ -n "$RELEASE_TEMP" ] && [ -d "$RELEASE_TEMP" ]; then
    rm -rf -- "$RELEASE_TEMP"
  fi
}
trap cleanup EXIT HUP INT TERM

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user; this invokes sudo for installation." >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }
git -C "$PROJECT_DIR" diff --quiet && git -C "$PROJECT_DIR" diff --cached --quiet || { echo "Refusing to update a checkout with uncommitted changes." >&2; exit 1; }
old=$(git -C "$PROJECT_DIR" rev-parse HEAD)
release_commit=
case "$TARGET" in
  v[0-9]*)
    command -v python3 >/dev/null 2>&1 || {
      echo "Signed release updates require Python 3." >&2
      exit 1
    }
    command -v mktemp >/dev/null 2>&1 || {
      echo "Signed release updates require mktemp." >&2
      exit 1
    }
    command -v gh >/dev/null 2>&1 || {
      echo "Signed release updates require GitHub CLI (gh)." >&2
      exit 1
    }
    RELEASE_TEMP=$(mktemp -d /tmp/outpost-release.XXXXXXXX)
    gh release download "$TARGET" --repo "$RELEASE_REPOSITORY" --dir "$RELEASE_TEMP"
    release_commit=$(python3 "$PROJECT_DIR/tools/verify_release.py" \
      --directory "$RELEASE_TEMP" --tag "$TARGET" --print-commit)
    for artifact in "$RELEASE_TEMP"/*; do
      [ -f "$artifact" ] || continue
      gh attestation verify "$artifact" --repo "$RELEASE_REPOSITORY"
    done
    echo "Verified signed release assets for $TARGET at $release_commit"
    ;;
  *)
    echo "Development update from $TARGET; signed release verification applies to v* tags."
    ;;
esac
git -C "$PROJECT_DIR" fetch --tags origin
target=$(git -C "$PROJECT_DIR" rev-parse --verify "$TARGET^{commit}")
if [ -n "$release_commit" ] && [ "$target" != "$release_commit" ]; then
  echo "Release metadata commit $release_commit does not match tag commit $target" >&2
  exit 1
fi
git -C "$PROJECT_DIR" checkout --detach "$target"
if sudo "$SCRIPT_DIR/install.sh"; then
  echo "Installed Git revision $target"
else
  git -C "$PROJECT_DIR" checkout --detach "$old"
  echo "Install failed; source checkout returned to $old" >&2
  exit 1
fi
