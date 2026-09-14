#!/usr/bin/env bash
# Skill/plugin smoke - helpers and mocked native calls only; never launches skills.py.
set -euo pipefail
ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[skill-fanout smoke] units"
python3 "$ENGINE/tests/test_fanout.py"
python3 "$ENGINE/tests/test_plugins.py"

echo "[skill-fanout smoke] public repo hygiene"
bad_private_store='\.private-'"data"
bad_ant='sk-'"ant-"
bad_cc='CLAUDE_CODE_'"OAUTH_TOKEN"
bad_api='ANTHROPIC_'"API_KEY"
if git -C "$ENGINE" grep --untracked -n -E "$bad_private_store|$bad_ant|$bad_cc|$bad_api"; then
  echo "FAIL: engine contains local path, private data path, or token-looking string" >&2
  exit 1
fi

echo "[skill-fanout smoke] PASS"
