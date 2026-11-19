#!/usr/bin/env bash
# Commit NBA_STATE_DIR/state-*.json onto the `state` branch.
# Does not check out that branch and does not touch the working tree.
# A no-op when the JSON is unchanged. Never force-pushes. Never prints secrets.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0

STATE_DIR="${NBA_STATE_DIR:?NBA_STATE_DIR is required}"
LOCK="${NBA_STATE_LOCK:-/tmp/nba-totals-state.lock}"
mkdir -p "$(dirname "$LOCK")"
exec 9>"$LOCK"
flock 9

shopt -s nullglob
files=()
for f in "$STATE_DIR"/state-*.json "$STATE_DIR"/checkpoints-*.json "$STATE_DIR"/grades-*.json "$STATE_DIR"/record.json "$STATE_DIR"/record-dryrun.json "$STATE_DIR"/parlays.json "$STATE_DIR"/parlays-dryrun.json "$STATE_DIR"/confirmed-lines.csv; do
  if [ -f "$f" ]; then
    files+=("$f")
  fi
done
if [ ${#files[@]} -eq 0 ]; then
  exit 0
fi

cd "$(git rev-parse --show-toplevel)"

export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-github-actions[bot]}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"
export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"

fetch_err=$(mktemp)
index=$(mktemp)
push_err=$(mktemp)
cleanup() { rm -f "$fetch_err" "$index" "$push_err"; }
trap cleanup EXIT

have_parent=0
if git fetch --quiet origin state 2>"$fetch_err"; then
  have_parent=1
else
  if ! grep -Eqi "couldn't find remote ref|remote ref .* not found|no matching remote" "$fetch_err"; then
    sed -E 's/ghs_[A-Za-z0-9_]+/[REDACTED]/g; s/ghp_[A-Za-z0-9_]+/[REDACTED]/g; s/x-access-token:[^@[:space:]]+/x-access-token:[REDACTED]/g' "$fetch_err" >&2
    exit 1
  fi
fi

export GIT_INDEX_FILE="$index"
git read-tree --empty
dates=""
for f in "${files[@]}"; do
  base=$(basename "$f")
  case "$base" in
    state-*.json|checkpoints-*.json|grades-*.json|record.json|record-dryrun.json|parlays.json|parlays-dryrun.json|confirmed-lines.csv) ;;
    *) continue ;;
  esac
  if grep -Eiq 'api[_-]?key|telegram_bot|x-access-token|/bot[0-9]+:' "$f"; then
    echo "refusing to commit ${base}: file looks like it contains a secret" >&2
    exit 1
  fi
  blob=$(git hash-object -w "$f")
  git update-index --add --cacheinfo "100644,$blob,$base"
  d=${base#state-}
  d=${d%.json}
  d=${d%-dryrun}
  dates="${dates:+$dates, }$d"
done
tree=$(git write-tree)

parents=()
if [ "$have_parent" -eq 1 ]; then
  parent=$(git rev-parse origin/state)
  old=$(git rev-parse "${parent}^{tree}")
  if [ "$old" = "$tree" ]; then
    exit 0
  fi
  parents+=(-p "$parent")
fi

commit=$(git commit-tree "$tree" "${parents[@]}" -m "Update NBA watcher state (${dates:-slate})")
if ! git push --quiet origin "${commit}:refs/heads/state" 2>"$push_err"; then
  sed -E 's/ghs_[A-Za-z0-9_]+/[REDACTED]/g; s/ghp_[A-Za-z0-9_]+/[REDACTED]/g; s/x-access-token:[^@[:space:]]+/x-access-token:[REDACTED]/g' "$push_err" >&2
  exit 1
fi
git update-ref refs/remotes/origin/state "$commit"
