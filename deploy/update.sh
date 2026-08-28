#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")
TARGET=${1:-origin/main}
RELEASE_REPOSITORY=${OUTPOST_RELEASE_REPOSITORY:-baal-bot/Outpost-Meshtastic}
ALLOW_UNVERIFIED_CI=${OUTPOST_ALLOW_UNVERIFIED_CI:-0}
GH=${OUTPOST_GH:-}
HAILORT_WHEEL=${OUTPOST_HAILORT_WHEEL:-}
HAILO_VLM_MODEL_SOURCE=${OUTPOST_HAILO_VLM_MODEL:-}
RELEASE_TEMP=

cleanup() {
  if [ -n "$RELEASE_TEMP" ] && [ -d "$RELEASE_TEMP" ]; then
    rm -rf -- "$RELEASE_TEMP"
  fi
}
trap cleanup EXIT HUP INT TERM

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user; this invokes sudo for installation." >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required" >&2; exit 1; }
if [ -z "$GH" ]; then
  GH=$(command -v gh 2>/dev/null || true)
fi
if [ -z "$GH" ] && [ -x /home/linuxbrew/.linuxbrew/bin/gh ]; then
  GH=/home/linuxbrew/.linuxbrew/bin/gh
fi
case "$ALLOW_UNVERIFIED_CI" in
  0|1) ;;
  *) echo "OUTPOST_ALLOW_UNVERIFIED_CI must be 0 or 1" >&2; exit 1 ;;
esac
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
    [ -n "$GH" ] && [ -x "$GH" ] || {
      echo "Signed release updates require GitHub CLI (gh)." >&2
      exit 1
    }
    RELEASE_TEMP=$(mktemp -d /tmp/outpost-release.XXXXXXXX)
    "$GH" release download "$TARGET" --repo "$RELEASE_REPOSITORY" --dir "$RELEASE_TEMP"
    release_commit=$(python3 "$PROJECT_DIR/tools/verify_release.py" \
      --directory "$RELEASE_TEMP" --tag "$TARGET" --print-commit)
    for artifact in "$RELEASE_TEMP"/*; do
      [ -f "$artifact" ] || continue
      "$GH" attestation verify "$artifact" --repo "$RELEASE_REPOSITORY"
    done
    echo "Verified signed release assets for $TARGET at $release_commit"
    ;;
  *)
    echo "Development update from $TARGET; signed release verification applies to v* tags."
    ;;
esac
if ! git -C "$PROJECT_DIR" fetch --tags origin; then
  if [ "$ALLOW_UNVERIFIED_CI" = 1 ]; then
    echo "WARNING: origin fetch failed; using only the already-local target revision." >&2
  else
    echo "Refusing to update without a successful origin fetch." >&2
    exit 1
  fi
fi
target=$(git -C "$PROJECT_DIR" rev-parse --verify "$TARGET^{commit}")
if [ -n "$release_commit" ] && [ "$target" != "$release_commit" ]; then
  echo "Release metadata commit $release_commit does not match tag commit $target" >&2
  exit 1
fi
ci_evidence=
if [ "$ALLOW_UNVERIFIED_CI" = 1 ]; then
  echo "WARNING: exact-commit CI verification bypassed by explicit operator override." >&2
else
  [ -n "$GH" ] && [ -x "$GH" ] || {
    echo "GitHub CLI (gh) is required to verify exact-commit CI. For an offline emergency only," >&2
    echo "set OUTPOST_ALLOW_UNVERIFIED_CI=1." >&2
    exit 1
  }
  ci_evidence=$("$GH" run list --repo "$RELEASE_REPOSITORY" --workflow ci.yml \
    --commit "$target" --limit 20 \
    --json headSha,status,conclusion,databaseId,url | \
    python3 "$PROJECT_DIR/tools/check_ci_evidence.py" --commit "$target") || {
      echo "Refusing to deploy $target without successful exact-commit CI." >&2
      echo "Wait for CI to pass, or use OUTPOST_ALLOW_UNVERIFIED_CI=1 only for an offline emergency." >&2
      exit 1
    }
  echo "Verified exact-commit CI: $ci_evidence"
fi
git -C "$PROJECT_DIR" checkout --detach "$target"
if sudo env OUTPOST_ALLOW_UNVERIFIED_CI="$ALLOW_UNVERIFIED_CI" \
  OUTPOST_CI_VERIFIED_REVISION="$target" OUTPOST_CI_EVIDENCE="$ci_evidence" \
  OUTPOST_HAILORT_WHEEL="$HAILORT_WHEEL" OUTPOST_HAILO_VLM_MODEL="$HAILO_VLM_MODEL_SOURCE" \
  "$SCRIPT_DIR/install.sh"; then
  echo "Installed Git revision $target"
else
  git -C "$PROJECT_DIR" checkout --detach "$old"
  echo "Install failed; source checkout returned to $old" >&2
  exit 1
fi
