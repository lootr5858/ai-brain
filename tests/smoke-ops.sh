#!/usr/bin/env bash
# Ops smoke - local ops dashboard, nudge, and backup-assist.
set -euo pipefail
ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRAIN="$(cd "$ENGINE/.." && pwd)"
cd "$ENGINE"

python3 tests/test_ops.py
python3 tests/test_dashboard.py
uv run python3 tests/test_dashboard_install.py
"$ENGINE/.venv/bin/python" tests/test_dashboard_brain_mcp.py
uv run python3 tests/test_dashboard_content.py
uv run python3 tests/test_dashboard_editing.py
python3 backup/brain/user/test_backup.py
python3 tests/test_backup_layout.py
python3 dashboard/server.py --once --fixture
python3 consolidation-nudge/nudge.py --config "$BRAIN/brain.config.toml" --dry-run
BRAIN_CONFIG="$BRAIN/brain.config.toml" python3 backup/assist.py status
manifest="${TMPDIR:-/tmp}/brain-os-ops-backup-manifest.json"
python3 backup/assist.py manifest --root "$BRAIN" > "$manifest"
python3 - "$manifest" <<'PY'
import json
import sys
from pathlib import Path
data = json.loads(Path(sys.argv[1]).read_text())
text = json.dumps(data).lower()
for forbidden in ("client_" + "secret", "private_" + "key", "seed " + "phrase", "token" + "=", "password" + "="):
    assert forbidden not in text, forbidden
print("ops manifest hygiene: PASS")
PY

bad_private_store='\.private-'"data"
bad_ant='sk-'"ant-"
bad_private='private_'"key"
bad_seed='seed '"phrase"
bad_pass='pass'"word="
source "$ENGINE/tests/public-hygiene.sh"
public_hygiene_check_controls
if rg -n "$bad_private_store|$bad_ant|$bad_private|$bad_seed|$bad_pass|$PUBLIC_HYGIENE_CREDENTIAL_LEAK" .; then
  echo "FAIL: engine contains local path, private data path, or token-looking string" >&2
  exit 1
fi
echo "ops smoke: PASS"
