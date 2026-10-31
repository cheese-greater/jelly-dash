#!/usr/bin/env bash
# Copy this repo to the Pi and run the installer there.
# Usage: ./deploy/push.sh [user@host]
# The target can also come from $JELLYDASH_TARGET or from deploy/target (one line, gitignored).
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-${JELLYDASH_TARGET:-}}"
if [[ -z "$TARGET" && -f "$SOURCE_DIR/deploy/target" ]]; then
	TARGET="$(head -n1 "$SOURCE_DIR/deploy/target" | tr -d '[:space:]')"
fi
if [[ -z "$TARGET" ]]; then
	echo "usage: $0 user@host   (or put user@host in deploy/target, or export JELLYDASH_TARGET)" >&2
	exit 1
fi

echo "==> copying to $TARGET:~/jelly-dash"
if command -v rsync >/dev/null; then
	rsync -az --delete --exclude '__pycache__' --exclude '.git' --exclude 'deploy/target' "$SOURCE_DIR/" "$TARGET:jelly-dash/"
else
	ssh "$TARGET" 'mkdir -p jelly-dash'
	scp -rq "$SOURCE_DIR"/{jellydash,deploy,config.example.toml,README.md} "$TARGET:jelly-dash/"
fi
echo "==> installing (sudo on the Pi)"
ssh -t "$TARGET" 'cd jelly-dash && sudo ./deploy/install.sh'
