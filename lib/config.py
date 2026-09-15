#!/usr/bin/env python3
"""Shared engine configuration loader."""
import os
from pathlib import Path
import tomllib


ENGINE = Path(__file__).resolve().parents[1]
CONFIG_ENV = "BRAIN_CONFIG"


def config_path(path: str | Path | None = None) -> Path:
    selected = path if path is not None else os.environ.get(CONFIG_ENV)
    return Path(os.path.expandvars(str(selected))).expanduser().resolve() if selected else ENGINE.parent / "brain.config.toml"


def load_config(path: str | Path | None = None) -> dict:
    selected = config_path(path)
    explicit = path is not None or CONFIG_ENV in os.environ
    if not selected.exists():
        if explicit:
            raise FileNotFoundError(f"brain config not found: {selected}")
        data = {}
    else:
        with selected.open("rb") as handle:
            data = tomllib.load(handle)

    paths = dict(data.get("paths", {}))

    def resolved(value: str | Path) -> Path:
        candidate = Path(os.path.expandvars(str(value))).expanduser()
        return (candidate if candidate.is_absolute() else selected.parent / candidate).resolve()

    brain = resolved(paths.get("brain", selected.parent))
    paths["brain"] = str(brain)
    for name in ("engine", "agents", "user"):
        paths[name] = str(resolved(paths.get(name, brain / name)))
    data["paths"] = paths
    fanout = data.get("engine", {}).get("fanout", {})
    if "targets" in fanout:
        fanout["targets"] = {
            name: str(resolved(value)) for name, value in fanout["targets"].items()
        }
    if "state" in fanout:
        fanout["state"] = str(resolved(fanout["state"]))
    plugins = fanout.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("engine.fanout.plugins must be a table")
    marketplace_names, marketplace_roots = set(), set()
    for target, options in plugins.items():
        if target not in fanout.get("targets", {}):
            raise ValueError(f"plugin runtime has no matching skill target: {target}")
        if not isinstance(options, dict) or options.get("runtime") not in ("claude-code", "codex"):
            raise ValueError(f"{target}: plugin runtime must be claude-code or codex")
        if options["runtime"] == "codex":
            from lib.plugins import identifier
            name = identifier(options.get("marketplace_name"), "marketplace")
            if not isinstance(options.get("marketplace"), str) or not options["marketplace"]:
                raise ValueError(f"{target}: marketplace directory is required")
            options["marketplace"] = str(resolved(options["marketplace"]))
            if name in marketplace_names or options["marketplace"] in marketplace_roots:
                raise ValueError("Codex plugin targets need distinct marketplace names and directories")
            marketplace_names.add(name)
            marketplace_roots.add(options["marketplace"])
            executable = options.get("executable", "codex")
            if not isinstance(executable, str) or not executable.strip():
                raise ValueError(f"{target}: executable must be a command name or path")
            if "/" in executable or executable.startswith(("~", "$")):
                options["executable"] = str(resolved(executable))
    return data
