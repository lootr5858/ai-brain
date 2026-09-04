#!/usr/bin/env python3
"""No-LLM proposal consolidation nudge."""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.ops import load_ops_config, proposal_summary


def message(agents: Path) -> str:
    buckets = proposal_summary(agents)
    total = sum(b.count for b in buckets)
    if total == 0:
        return "No proposals pending"
    oldest = min(b.oldest_mtime for b in buckets if b.oldest_mtime is not None)
    age_days = int((time.time() - oldest) // 86400)
    by_agent = ", ".join(f"{b.agent}: {b.count}" for b in buckets if b.count)
    return f"{total} proposal(s) pending; oldest {age_days} day(s); {by_agent}"


def main() -> int:
    ap = argparse.ArgumentParser()
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--config")
    source.add_argument("--root", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    selected = args.config or (args.root.resolve() / "brain.config.toml" if args.root else None)
    cfg = load_ops_config(selected)
    msg = message(cfg.agents)
    if msg == "No proposals pending" or not cfg.notify_command or args.dry_run:
        print(msg)
        return 0
    subprocess.run([*shlex.split(cfg.notify_command), msg], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
