#!/usr/bin/env python3
"""Install a brain router and global agent adapters. Default is dry-run."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.config import load_config
from lib.session_entry import (
    agents_adapter,
    claude_adapter,
    expanded_path,
    merged_adapter,
    root_bootstrap,
    write_text_if_changed,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config")
    ap.add_argument("--brain", type=Path, help="Brain directory; defaults to paths.brain in config")
    ap.add_argument("--agents-root", type=Path, help="Agent store; defaults to paths.agents")
    users = ap.add_mutually_exclusive_group()
    users.add_argument("--user-root", type=Path, help="One user store; requires README.md")
    users.add_argument("--no-user", action="store_true", help="Load agent guidance only")
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or "~/.codex",
                    help="Codex directory (default: CODEX_HOME or ~/.codex)")
    ap.add_argument("--claude-home", default=os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude",
                    help="Claude directory (default: CLAUDE_CONFIG_DIR or ~/.claude)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    brain = expanded_path(a.brain or cfg["paths"]["brain"])
    agents = expanded_path(a.agents_root or (brain / "agents" if a.brain else cfg["paths"]["agents"]))
    user = expanded_path(a.user_root or (brain / "user" if a.brain else cfg["paths"]["user"]))
    if a.user_root and not (user / "README.md").is_file():
        ap.error("--user-root requires README.md")
    if a.no_user or not (user / "README.md").is_file():
        user = None
    codex = expanded_path(a.codex_home)
    claude = expanded_path(a.claude_home)
    apply = a.apply and not a.dry_run

    try:
        actions = [
            (codex / "AGENTS.md", merged_adapter(codex / "AGENTS.md", agents_adapter(agents, user))),
            (claude / "CLAUDE.md", merged_adapter(claude / "CLAUDE.md", claude_adapter(agents, user))),
        ]
    except ValueError as exc:
        ap.error(str(exc))
    if not (brain / "BOOTSTRAP.md").exists():
        actions.insert(0, (brain / "BOOTSTRAP.md", root_bootstrap()))
    report = []
    override = codex / "AGENTS.override.md"
    if override.is_file() and override.read_text().strip():
        report.append(f"WARN {override} takes precedence over AGENTS.md; include the brain entry there or remove the override")
    for path, text in actions:
        if write_text_if_changed(path, text, apply):
            report.append(f"WRITE {path}")
    if not apply:
        report.append("INFO dry-run only; pass --apply to write")
    print("\n".join(report) or "nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
