"""Write the brain entry block and context hooks into Claude Code and Codex; optionally the server unit."""
from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

START = "<!-- brain-engine:bootstrap:start -->"
END = "<!-- brain-engine:bootstrap:end -->"
ENTRY = ("Brain context: `{agents}/README.md`{user}, plus the files they list as always loaded. "
         "A session hook injects them; if they are not in context, read and follow them.")
MARKS = ("bin/brain-context", " -m brain hook ")  # old chunked hooks, current hooks
UNIT = """[Unit]
Description=brain remote MCP server
After=network-online.target

[Service]
ExecStart={command}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def expanded(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


def entry(agents: Path, user: Path | None) -> str:
    """Global instruction block shared by every runtime; hooks do the loading."""
    return ENTRY.format(agents=agents, user=f" and `{user}/README.md`" if user else "")


def hook_groups(brain: Path) -> dict[str, list[dict]]:
    """One hook per event; the commands never change when store files move.

    `-P` keeps the working directory off sys.path, so a project's own `brain` package can't run instead.
    """
    base = f"{shlex.quote(sys.executable)} -P -m brain hook"

    def group(name: str) -> list[dict]:
        command = f"{base} {name} --brain {shlex.quote(str(brain))}"
        return [{"hooks": [{"type": "command", "command": command}]}]

    return {"SessionStart": group("session"), "SubagentStart": group("subagent")}


def merged_hooks(path: Path, groups: dict[str, list[dict]]) -> str:
    """Replace our hook groups in a Claude settings or Codex hooks file; keep the rest."""
    current = path.read_text() if path.exists() else ""
    data = json.loads(current) if current.strip() else {}
    hooks = data.setdefault("hooks", {})
    for event, ours in groups.items():
        kept = [
            group for group in hooks.get(event, [])
            if not any(mark in hook.get("command", "") for hook in group.get("hooks", []) for mark in MARKS)
        ]
        hooks[event] = kept + ours
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def merged_block(path: Path, text: str) -> str:
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


def unit(brain: Path, public_url: str, cf_team: str, cf_aud: str, port: int) -> str:
    """A systemd user unit for `brain serve`; starting it stays the owner's step."""
    command = shlex.join([sys.executable, "-P", "-m", "brain", "serve", "--brain", str(brain), "--public-url", public_url,
                          "--cf-team", cf_team, "--cf-aud", cf_aud, "--port", str(port)])
    return UNIT.format(command=command)


def unit_path() -> Path:
    return expanded(os.environ.get("XDG_CONFIG_HOME") or "~/.config") / "systemd" / "user" / "brain.service"


def write_if_changed(path: Path, text: str, apply: bool) -> bool:
    if path.exists() and path.read_text() == text:
        return False
    if apply:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return True


def run(brain: Path, claude_home: Path, codex_home: Path, apply: bool, web: tuple | None = None) -> list[str]:
    """Plan or apply the writes; `web` = (public URL, CF team, CF AUD, port) adds the server unit. Return report lines."""
    brain = expanded(brain)
    agents, user = brain / "agents", brain / "user"
    if not (agents / "README.md").is_file():
        raise ValueError(f"no agents/README.md under {brain}; pass --brain")
    if not (user / "README.md").is_file():
        user = None
    claude, codex = expanded(claude_home), expanded(codex_home)
    block, groups = entry(agents, user), hook_groups(brain)
    actions = [
        (codex / "AGENTS.md", merged_block(codex / "AGENTS.md", block)),
        (claude / "CLAUDE.md", merged_block(claude / "CLAUDE.md", block)),
        (codex / "hooks.json", merged_hooks(codex / "hooks.json", groups)),
        (claude / "settings.json", merged_hooks(claude / "settings.json", groups)),
    ]
    if web:
        actions.append((unit_path(), unit(brain, *web)))
    report = []
    override = codex / "AGENTS.override.md"
    if override.is_file() and override.read_text().strip():
        report.append(f"WARN {override} takes precedence over AGENTS.md; include the brain entry there or remove the override")
    for path, text in actions:
        if write_if_changed(path, text, apply):
            report.append(f"WRITE {path}")
            if path == codex / "hooks.json":
                report.append("INFO Codex asks you to trust new or changed hooks before running them")
            if path == unit_path():
                report.append("INFO start it: systemctl --user daemon-reload && systemctl --user enable --now brain.service")
    if not apply:
        report.append("INFO dry-run only; pass --apply to write")
    return report or ["nothing to do"]
