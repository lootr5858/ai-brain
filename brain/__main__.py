"""brain: load the brain's always-loaded context into local agents."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from brain import context

BRAIN = Path(__file__).resolve().parents[2]  # engine checkout's parent holds agents/ and user/


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--brain", type=Path, default=BRAIN, help=f"brain root holding agents/ and user/ (default: {BRAIN})")
    ap = argparse.ArgumentParser(prog="brain", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    hook = sub.add_parser("hook", parents=[common], help="print a context set for a SessionStart or SubagentStart hook")
    hook.add_argument("set", choices=context.SETS)
    install = sub.add_parser("install", parents=[common], help="write entry blocks and hooks; dry-run unless --apply")
    install.add_argument("--apply", action="store_true")
    install.add_argument("--claude-home", default=os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude",
                         help="Claude directory (default: CLAUDE_CONFIG_DIR or ~/.claude)")
    install.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or "~/.codex",
                         help="Codex directory (default: CODEX_HOME or ~/.codex)")
    a = ap.parse_args(argv)

    if a.command == "hook":
        user = a.brain / "user"
        out = context.text(a.brain / "agents", user if user.is_dir() else None, a.set)
        if out and a.set == "subagent":
            out = json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": out}})
        if out:
            print(out)
        return 0

    from brain import install as installer

    try:
        report = installer.run(a.brain, a.claude_home, a.codex_home, a.apply)
    except ValueError as exc:
        ap.error(str(exc))
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
