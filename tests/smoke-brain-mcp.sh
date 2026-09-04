#!/usr/bin/env bash
set -euo pipefail
ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ENGINE/.venv/bin/python"
"$PYTHON" "$ENGINE/tests/test_brain_mcp.py"
"$PYTHON" "$ENGINE/tests/test_brain_mcp_protocol.py"
"$PYTHON" "$ENGINE/tests/test_brain_mcp_stdio.py"
"$PYTHON" "$ENGINE/tests/test_brain_mcp_remote.py"
bad_private_store='\.private-'"data"
bad_ant='sk-'"ant-"
source "$ENGINE/tests/public-hygiene.sh"
public_hygiene_check_controls
if git -C "$ENGINE" grep --untracked -n -E "$bad_private_store|$bad_ant|$PUBLIC_HYGIENE_CREDENTIAL_LEAK"; then
  echo "FAIL: engine contains local path, private data path, or token-looking string" >&2
  exit 1
fi
echo "brain-mcp smoke: PASS"
