#!/usr/bin/env bash
# Scaffold smoke test.
# Asserts the scaffold deliverables exist and that brain.config.toml parses with the
# expected structure. Fails loudly if the scaffold is broken. No framework.
set -euo pipefail

ENGINE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRAIN="$(cd "$ENGINE/.." && pwd)"
fail=0
check() { if eval "$2"; then echo "  ok   $1"; else echo "  FAIL $1" >&2; fail=1; fi; }

echo "[scaffold smoke] engine=$ENGINE"
check "BOOTSTRAP.md exists"   "[ -f '$ENGINE/BOOTSTRAP.md' ]"
check "README.md exists"      "[ -f '$ENGINE/README.md' ]"
check "brain.config.toml exists" "[ -f '$BRAIN/brain.config.toml' ]"

# Config parses as TOML, carries live sections, and contains no retired routing state.
check "brain.config.toml contains only live engine config" \
  "python3 -c \"import tomllib,sys; d=tomllib.load(open('$BRAIN/brain.config.toml','rb')); e=d.get('engine',{}); ok=d.get('paths',{}).get('engine') and e.get('fanout',{}).get('targets') and 'roster' not in d and 'file_triggers' not in e; sys.exit(0 if ok else 1)\""

check "README.md is the human guide" \
  "grep -q '^# AI Brain Engine$' '$ENGINE/README.md'"

if [ "$fail" -eq 0 ]; then echo "[scaffold smoke] PASS"; else echo "[scaffold smoke] FAIL" >&2; exit 1; fi
