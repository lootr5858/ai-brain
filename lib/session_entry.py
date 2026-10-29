#!/usr/bin/env python3
"""Session-entry helpers for the root router and runtime adapters."""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from lib.context import CHUNKS

ENGINE = Path(__file__).resolve().parents[1]
TEMPLATES = ENGINE / "templates" / "root"
START = "<!-- brain-engine:bootstrap:start -->"
END = "<!-- brain-engine:bootstrap:end -->"
HOOK_MARK = "bin/brain-context"


def expanded_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


def root_bootstrap() -> str:
    return (TEMPLATES / "BOOTSTRAP.md").read_text()


def entry_adapter(agents: str | Path, user: str | Path | None = None) -> str:
    """Global instruction block shared by every runtime; hooks do the loading."""
    text = (TEMPLATES / "ENTRY.md").read_text()
    if user is None:
        text = text.replace(' and `$USER_ROOT/README.md`', '')
    else:
        text = text.replace("$USER_ROOT", str(expanded_path(user)))
    return text.replace("$AGENTS_ROOT", str(expanded_path(agents)))


def hook_groups(engine: str | Path, config: str | Path | None = None) -> dict[str, list[dict]]:
    """Fixed hook commands; store layout changes never alter them."""
    command = shlex.quote(str(expanded_path(engine) / HOOK_MARK))
    if config is not None:
        command += " --config " + shlex.quote(str(expanded_path(config)))

    def groups(arguments: str) -> list[dict]:
        return [
            {"hooks": [{"type": "command", "command": f"{command} {arguments.format(chunk=chunk)}"}]}
            for chunk in range(1, CHUNKS + 1)
        ]

    return {
        "SessionStart": groups("session --chunk {chunk}"),
        "SubagentStart": groups("subagent --chunk {chunk} --json SubagentStart"),
    }


def merged_hooks(path: Path, groups: dict[str, list[dict]]) -> str:
    """Replace our hook groups in a Claude settings or Codex hooks file; keep the rest."""
    current = path.read_text() if path.exists() else ""
    data = json.loads(current) if current.strip() else {}
    hooks = data.setdefault("hooks", {})
    for event, ours in groups.items():
        kept = [
            group for group in hooks.get(event, [])
            if not any(HOOK_MARK in hook.get("command", "") for hook in group.get("hooks", []))
        ]
        hooks[event] = kept + ours
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def merged_adapter(path: Path, text: str) -> str:
    """Replace our block while preserving unrelated global instructions."""
    current = path.read_text() if path.exists() else ""
    block = f"{START}\n{text.rstrip()}\n{END}"
    if START in current or END in current:
        if current.count(START) != 1 or current.count(END) != 1 or current.index(START) > current.index(END):
            raise ValueError(f"malformed brain-engine block in {path}")
        start = current.index(START)
        end = current.index(END) + len(END)
        return current[:start] + block + current[end:]
    separator = "\n\n" if current and not current.endswith("\n") else "\n" if current else ""
    return current + separator + block + "\n"


def write_text_if_changed(path: Path, text: str, apply: bool) -> bool:
    if path.exists() and path.read_text() == text:
        return False
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return True
