#!/usr/bin/env python3
"""Shared engine configuration loader."""
import os
from pathlib import Path
import tomllib


ENGINE = Path(__file__).resolve().parents[1]
CONFIG_ENV = "BRAIN_CONFIG"


def config_path(path: str | Path | None = None) -> Path:
    selected = path if path is not None else os.environ.get(CONFIG_ENV)
    return Path(selected).expanduser().resolve() if selected else ENGINE.parent / "brain.config.toml"


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
        candidate = Path(value).expanduser()
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
    return data
