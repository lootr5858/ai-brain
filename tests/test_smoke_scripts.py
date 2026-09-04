#!/usr/bin/env python3
"""Aggregate smoke script shape checks."""
from pathlib import Path


root = Path(__file__).resolve().parents[1]
aggregate = (root / "tests" / "smoke.sh").read_text()
expected = (
    "smoke-scaffold.sh",
    "smoke-skill-fanout.sh",
    "smoke-ops.sh",
    "smoke-brain-mcp.sh",
    "smoke-session-entry.sh",
)
for name in expected:
    assert name in aggregate, f"aggregate smoke is missing {name}"
    assert (root / "tests" / name).is_file(), f"missing smoke script: {name}"

print("smoke script tests: PASS")
