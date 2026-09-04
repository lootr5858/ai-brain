#!/usr/bin/env bash
set -euo pipefail

ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$ENGINE/.venv/bin:$PATH"

SEEN_SMOKES=""
for smoke in \
  smoke-scaffold.sh \
  smoke-skill-fanout.sh \
  smoke-ops.sh \
  smoke-brain-mcp.sh \
  smoke-session-entry.sh
do
  if [[ " $SEEN_SMOKES " == *" $smoke "* ]]; then
    echo "FAIL: duplicate child smoke runner: $smoke" >&2
    exit 1
  fi
  SEEN_SMOKES="$SEEN_SMOKES $smoke"
  bash "$ENGINE/tests/$smoke"
done
