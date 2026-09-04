#!/usr/bin/env python3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "backup"


allowed = {"assist.py", "brain"}
entries = {p.name for p in BACKUP.iterdir() if p.name != "__pycache__"}
unexpected = sorted(entries - allowed)

assert not unexpected, f"runtime-specific backup does not belong in engine/backup: {unexpected}"
assert (BACKUP / "assist.py").is_file()
assert (BACKUP / "brain").is_dir()

print("backup layout guard: PASS")
