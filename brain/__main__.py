"""brain: load the brain's always-loaded context into local agents; serve it to remote ones."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from brain import context

BRAIN = Path(__file__).resolve().parents[2]  # engine checkout's parent holds agents/ and user/
PORT = 8787


def public_url(value: str) -> str:
    """The tunnel's origin. OAuth names it in every token, so a bad one must stop here, not at start."""
    try:
        parts = urlsplit(value)
        ok = parts.scheme == "https" and parts.hostname and not (parts.path.strip("/") or parts.query or parts.fragment)
    except ValueError:
        ok = False
    if not ok:
        raise argparse.ArgumentTypeError(f"needs https://HOST, for example https://brain.example.com (got {value!r})")
    return value


def web_flags(parser: argparse.ArgumentParser, required: bool) -> None:
    parser.add_argument("--public-url", type=public_url, required=required, help="HTTPS URL the tunnel serves: https://HOST")
    parser.add_argument("--cf-team", required=required, help="Cloudflare Access team domain: TEAM.cloudflareaccess.com")
    parser.add_argument("--cf-aud", required=required, help="Cloudflare Access application AUD tag")
    parser.add_argument("--port", type=int, default=PORT, help=f"loopback port (default: {PORT})")


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
    web_flags(install, required=False)
    serve = sub.add_parser("serve", parents=[common], help="run the remote MCP server and dashboard on loopback")
    web_flags(serve, required=True)
    a = ap.parse_args(argv)

    if a.command == "hook":
        user = a.brain / "user"
        out = context.text(a.brain / "agents", user if user.is_dir() else None, a.set)
        if out and a.set == "subagent":
            out = json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": out}})
        if out:
            print(out)
        return 0

    if a.command == "serve":
        from brain import app

        app.run(a.brain, a.public_url, a.cf_team, a.cf_aud, a.port)
        return 0

    from brain import install as installer

    web = (a.public_url, a.cf_team, a.cf_aud)
    if any(web) and not all(web):
        ap.error("--public-url, --cf-team, and --cf-aud go together")
    try:
        report = installer.run(a.brain, a.claude_home, a.codex_home, a.apply, (*web, a.port) if all(web) else None)
    except ValueError as exc:
        ap.error(str(exc))
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
