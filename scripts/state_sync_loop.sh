#!/usr/bin/env bash
# Push slate state every NBA_STATE_SYNC_S seconds while the watcher is polling.
# A killed job therefore loses at most one interval of state.
set -u
dir=$(dirname "$0")
interval="${NBA_STATE_SYNC_S:-30}"
while true; do
  sleep "$interval" || exit 0
  bash "$dir/persist_state.sh" || echo "state sync failed (will retry)" >&2
done
