#!/usr/bin/env bash
set -euo pipefail
ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$ENGINE/tests/test_session_entry.py"
bad_private_store='\.private-'"data"
bad_ant='sk-'"ant-"
bad_cc='CLAUDE_CODE_'"OAUTH_TOKEN"
bad_api='ANTHROPIC_'"API_KEY"
bad_role_secret='BRAIN_.*TOK'"EN=.*[A-Za-z0-9_-]{16,}"
if git -C "$ENGINE" grep --untracked -n -E "$bad_private_store|$bad_ant|$bad_cc|$bad_api|$bad_role_secret"; then
  echo "FAIL: engine contains local path, private data path, or token-looking string" >&2
  exit 1
fi
echo "session-entry smoke: PASS"
