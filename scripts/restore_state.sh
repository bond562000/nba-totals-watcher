#!/usr/bin/env bash
# Copy state-*.json from the dedicated `state` branch into NBA_STATE_DIR.
# Missing branch is fine (first night). Never prints file contents or the remote URL.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0

STATE_DIR="${NBA_STATE_DIR:?NBA_STATE_DIR is required}"
mkdir -p "$STATE_DIR"
cd "$(git rev-parse --show-toplevel)"

fetch_err=$(mktemp)
trap 'rm -f "$fetch_err"' EXIT
if ! git fetch --quiet origin state 2>"$fetch_err"; then
  if grep -Eqi "couldn't find remote ref|remote ref .* not found|no matching remote" "$fetch_err"; then
    exit 0
  fi
  sed -E 's/ghs_[A-Za-z0-9_]+/[REDACTED]/g; s/ghp_[A-Za-z0-9_]+/[REDACTED]/g; s/x-access-token:[^@[:space:]]+/x-access-token:[REDACTED]/g' "$fetch_err" >&2
  exit 1
fi

stage=$(mktemp -d)
git archive origin/state | tar -x -C "$stage"
shopt -s nullglob
for f in "$stage"/state-*.json "$stage"/checkpoints-*.json "$stage"/grades-*.json "$stage"/record.json "$stage"/record-dryrun.json "$stage"/confirmed-lines.csv; do
  if [ -f "$f" ]; then
    cp "$f" "$STATE_DIR/"
  fi
done
rm -rf "$stage"
