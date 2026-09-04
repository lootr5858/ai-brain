#!/usr/bin/env python3
"""Root adapter installer. Default is dry-run; writes require --apply."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.config import load_config
from lib.session_entry import (
    agents_adapter,
    claude_adapter,
    root_bootstrap,
    write_text_if_changed,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config")
    ap.add_argument("--brain", type=Path)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    brain = a.brain or Path(cfg["paths"]["brain"])
    apply = a.apply and not a.dry_run

    actions = [
        (brain / "BOOTSTRAP.md", root_bootstrap()),
        (brain / "AGENTS.md", agents_adapter()),
        (brain / "CLAUDE.md", claude_adapter()),
    ]
    report = []
    for path, text in actions:
        if write_text_if_changed(path, text, apply):
            report.append(f"WRITE {path}")
    if not apply:
        report.append("INFO dry-run only; pass --apply to write")
    print("\n".join(report) or "nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
