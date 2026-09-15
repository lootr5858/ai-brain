"""Plugin discovery and native Codex adapter. Importing this module has no effects."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from lib.fanout import parse_frontmatter

MANIFESTS = {"claude-code": ".claude-plugin/plugin.json",
             "codex": ".codex-plugin/plugin.json"}
IGNORED = {".git", "__pycache__", ".DS_Store", ".brain-managed"}


def identifier(value, label="plugin"):
    pattern = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*" if label == "plugin" else r"[A-Za-z0-9_-]+"
    if not isinstance(value, str) or not re.fullmatch(pattern, value) or value.startswith("-"):
        raise ValueError(f"invalid {label} name: {value!r}")
    return value


def read_object(path):
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def inside(root, value):
    if not isinstance(value, str) or not value.startswith("./"):
        raise ValueError(f"plugin resource must start with ./: {value!r}")
    path = root / value
    if not path.resolve().is_relative_to(root.resolve()) or not path.exists():
        raise ValueError(f"missing or escaping plugin resource: {path}")
    return path


def tree_entries(root):
    """Stable inventory; do not follow directory links or include repository metadata."""
    root = root.resolve()
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED)
        for name in sorted(dirs + [f for f in files if f not in IGNORED]):
            path = Path(directory) / name
            if path.is_symlink():
                target = path.resolve(strict=True)
                if not target.is_relative_to(root):
                    raise ValueError(f"plugin symlink escapes its root: {path}")
            elif not path.is_dir() and not path.is_file():
                raise ValueError(f"unsupported plugin filesystem entry: {path}")
            yield path


def fingerprint(root):
    root = root.resolve()
    digest = hashlib.sha256()
    for path in tree_entries(root):
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        if path.is_symlink():
            digest.update(b"L" + str(path.resolve().relative_to(root)).encode())
        elif path.is_file():
            digest.update(b"F" + str(path.stat().st_mode & 0o111).encode())
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
        else:
            digest.update(b"D")
        digest.update(b"\0")
    return digest.hexdigest()


def copy_plugin(source, destination):
    """Copy a self-contained snapshot, preserving links only within the plugin."""
    source = source.resolve()
    entries = list(tree_entries(source))
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns(*IGNORED))
    for path in entries:
        if path.is_symlink():
            copied = destination / path.relative_to(source)
            target = destination / path.resolve().relative_to(source)
            copied.unlink()
            copied.symlink_to(os.path.relpath(target, copied.parent))


@dataclass(frozen=True)
class Plugin:
    name: str
    root: Path
    runtime: str
    skills: tuple[str, ...]
    hooks: tuple[str, ...]
    digest: str


def hook_events(root, declaration):
    events = set()
    for item in declaration if isinstance(declaration, list) else [declaration]:
        obj = read_object(inside(root, item)) if isinstance(item, str) else item
        if not isinstance(obj, dict) or not isinstance(obj.get("hooks"), dict):
            raise ValueError("hook declaration must contain a hooks object")
        for event, groups in obj["hooks"].items():
            if not isinstance(event, str) or not isinstance(groups, list):
                raise ValueError("hook events must map to arrays")
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise ValueError(f"{event}: expected hook groups")
                for hook in group["hooks"]:
                    if not isinstance(hook, dict) or not isinstance(hook.get("type"), str):
                        raise ValueError(f"{event}: hook requires a type")
                    if hook["type"] == "command":
                        command = hook.get("command")
                        if not isinstance(command, str) or not command.strip():
                            raise ValueError(f"{event}: command hook requires a command")
                        # Validate explicit bundled script/resource references without running them.
                        for ref in re.findall(r"\$\{(?:CLAUDE_PLUGIN_ROOT|PLUGIN_ROOT)\}(/[^\s\"']+)", command):
                            inside(root, "." + ref)
            events.add(event)
    return tuple(sorted(events))


def read_plugin(root, runtime):
    manifest = read_object(root / MANIFESTS[runtime])
    name = identifier(manifest.get("name"))
    skill_dirs = set()
    paths = manifest.get("skills", "./skills/" if (root / "skills").is_dir() else [])
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        raise ValueError("skills must be a path or an array of paths")
    for value in paths:
        path = inside(root, value)
        if not path.is_dir():
            raise ValueError(f"skill path must be a directory: {path}")
        if (path / "SKILL.md").is_file():
            skill_dirs.add(path)
        else:
            skill_dirs.update(p.parent for p in path.rglob("SKILL.md")
                              if not set(p.relative_to(root).parts) & IGNORED)
    if not paths and (root / "SKILL.md").is_file():
        skill_dirs.add(root)
    names = []
    for directory in sorted(skill_dirs):
        if not directory.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"skill escapes plugin: {directory}")
        fm, _ = parse_frontmatter(directory / "SKILL.md")
        if not fm.get("name") or not fm.get("description"):
            raise ValueError(f"{directory}: skill requires name and description")
        names.append(fm["name"])
    if len(names) != len(set(names)):
        raise ValueError(f"{name}: duplicate bundled skill names")
    hooks = ()
    declaration = manifest.get("hooks")
    if declaration is None and (root / "hooks/hooks.json").exists():
        declaration = "./hooks/hooks.json"
    if declaration is not None:
        hooks = hook_events(root, declaration)
    return Plugin(name, root.resolve(), runtime, tuple(names), hooks, fingerprint(root))


def discover_plugins(agents, runtime, *, exclude=()):
    root = agents / "plugins"
    found, reports = {}, []
    if not root.exists():
        return found, reports
    duplicates = set()
    for directory in sorted(root.iterdir()):
        if directory.name.startswith(".") or not directory.is_dir():
            continue
        if directory.name in exclude:
            reports.append(f"SKIP {runtime} {directory.name}: excluded by configuration")
            continue
        if not (directory / MANIFESTS[runtime]).exists():
            reports.append(f"SKIP {runtime} {directory.name}: no {MANIFESTS[runtime]}")
            continue
        try:
            plugin = read_plugin(directory, runtime)
            if plugin.name in found or plugin.name in duplicates:
                found.pop(plugin.name, None)
                duplicates.add(plugin.name)
                raise ValueError(f"duplicate plugin name: {plugin.name}")
            found[plugin.name] = plugin
        except (ValueError, OSError, AssertionError) as exc:
            reports.append(f"REFUSE {runtime} {directory.name}: {exc}")
    return found, reports


class Codex:
    """Native calls are injected in tests; never called by a dry-run plan."""
    def __init__(self, executable="codex", runner=None):
        self.executable = executable
        self.runner = runner or subprocess.run

    def call(self, *args):
        command = [self.executable, "plugin", *args, "--json"]
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=120,
                                 check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Codex command failed: {args}: {exc}") from exc
        if result.returncode:
            raise RuntimeError(f"Codex command failed: {args}: {result.stderr.strip()}")
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise RuntimeError(f"Codex returned invalid JSON for {args}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"Codex returned an unexpected response for {args}")
        return value

    def inventory(self):
        markets = self.call("marketplace", "list").get("marketplaces")
        installed = self.call("list").get("installed")
        if not isinstance(markets, list) or not isinstance(installed, list):
            raise RuntimeError("unrecognized Codex inventory; no changes made")
        if any(not isinstance(m, dict) or not isinstance(m.get("name"), str)
               or not isinstance(m.get("root"), str) for m in markets):
            raise RuntimeError("unrecognized Codex marketplace entry")
        if any(not isinstance(p, dict) or not isinstance(p.get("name"), str)
               or not isinstance(p.get("marketplaceName"), str) for p in installed):
            raise RuntimeError("unrecognized Codex installed plugin entry")
        return markets, installed
