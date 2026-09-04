#!/usr/bin/env python3
"""Session-entry helpers for the root router and runtime adapters."""
from __future__ import annotations

from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
TEMPLATES = ENGINE / "templates" / "root"


def root_bootstrap() -> str:
    return (TEMPLATES / "BOOTSTRAP.md").read_text()


def agents_adapter() -> str:
    return (TEMPLATES / "AGENTS.md").read_text()


def claude_adapter() -> str:
    return (TEMPLATES / "CLAUDE.md").read_text()


def write_text_if_changed(path: Path, text: str, apply: bool) -> bool:
    if path.exists() and path.read_text() == text:
        return False
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return True
