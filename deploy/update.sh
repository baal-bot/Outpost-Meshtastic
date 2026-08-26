#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(dirname -- "$SCRIPT_DIR")
TARGET=${1:-origin/main}

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user; this invokes sudo for installation." >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }
git -C "$PROJECT_DIR" diff --quiet && git -C "$PROJECT_DIR" diff --cached --quiet || { echo "Refusing to update a checkout with uncommitted changes." >&2; exit 1; }
old=$(git -C "$PROJECT_DIR" rev-parse HEAD)
git -C "$PROJECT_DIR" fetch --tags origin
target=$(git -C "$PROJECT_DIR" rev-parse --verify "$TARGET^{commit}")
git -C "$PROJECT_DIR" checkout --detach "$target"
if sudo "$SCRIPT_DIR/install.sh"; then
  echo "Installed Git revision $target"
else
  git -C "$PROJECT_DIR" checkout --detach "$old"
  echo "Install failed; source checkout returned to $old" >&2
  exit 1
fi
