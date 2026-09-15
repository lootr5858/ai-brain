#!/usr/bin/env python3
"""Session-entry helpers for the root router and runtime adapters."""
from __future__ import annotations

import os
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
TEMPLATES = ENGINE / "templates" / "root"
START = "<!-- brain-engine:bootstrap:start -->"
END = "<!-- brain-engine:bootstrap:end -->"


def expanded_path(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


def root_bootstrap() -> str:
    return (TEMPLATES / "BOOTSTRAP.md").read_text()


def adapter(template: str, agents: str | Path, user: str | Path | None = None) -> str:
    text = (TEMPLATES / template).read_text()
    roots = {"$AGENTS_ROOT": expanded_path(agents)}
    if user is None:
        text = text.replace(' and `$USER_ROOT/BOOTSTRAP.md`', '')
        text = text.replace('@$USER_ROOT/BOOTSTRAP.md\n', '')
    else:
        roots["$USER_ROOT"] = expanded_path(user)
    for token, root in roots.items():
        if template == "CLAUDE.md" and any(char.isspace() for char in str(root)):
            raise ValueError("Claude @ imports require a bootstrap path without whitespace")
        text = text.replace(token, str(root))
    return text


def agents_adapter(agents: str | Path, user: str | Path | None = None) -> str:
    return adapter("AGENTS.md", agents, user)


def claude_adapter(agents: str | Path, user: str | Path | None = None) -> str:
    return adapter("CLAUDE.md", agents, user)


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
