#!/usr/bin/env python3
"""Read-only local brain-mcp domain tools."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re
import subprocess

from lib.config import load_config as load_engine_config


ENGINE = Path(__file__).resolve().parents[1]
MAX_TEXT = 50_000
READ_TOOLS = {
    "health_check",
    "list_context",
    "list_nonprivate",
    "read_nonprivate",
    "get_core",
    "get_discipline",
    "get_skill",
    "get_user_context",
    "get_reference",
    "search",
    "status",
}
class ToolError(Exception):
    pass


@dataclass(frozen=True)
class BrainMcpConfig:
    brain: Path
    engine: Path
    agents: Path
    user: Path


def load_config(config_path: str | Path | None = None) -> BrainMcpConfig:
    paths = load_engine_config(config_path)["paths"]
    return BrainMcpConfig(
        brain=Path(paths["brain"]),
        engine=Path(paths["engine"]),
        agents=Path(paths["agents"]),
        user=Path(paths["user"]),
    )


def tools() -> list[str]:
    return sorted(READ_TOOLS)


def call_tool(
    name: str,
    args: dict | None,
    cfg: BrainMcpConfig | None = None,
) -> str:
    args = args or {}
    cfg = cfg or load_config()
    if name not in READ_TOOLS:
        raise ToolError(f"unknown tool: {name}")
    if name == "health_check":
        return "pong from brain-mcp"
    if name == "list_context":
        return list_context(cfg)
    if name == "list_nonprivate":
        return list_nonprivate(cfg, args.get("path", "."))
    if name == "read_nonprivate":
        return read_nonprivate(cfg, args.get("path", ""))
    if name == "get_core":
        return get_core(cfg)
    if name == "get_discipline":
        return get_discipline(cfg, args.get("name", ""))
    if name == "get_skill":
        return get_skill(cfg, args.get("name", ""))
    if name == "get_user_context":
        return get_user_context(cfg, args.get("field", ""))
    if name == "get_reference":
        return get_reference(cfg, args.get("path", ""))
    if name == "search":
        return search(cfg, args.get("query", ""))
    if name == "status":
        return json.dumps({"transport": "stdio", "tools": tools()}, indent=2)
    raise ToolError(f"unknown tool: {name}")


def _reject_rel(rel: str) -> Path:
    path = Path(str(rel or ""))
    if not rel or path.is_absolute() or ".." in path.parts:
        raise ToolError("path is not exposed")
    return path


def _safe_existing(base: Path, rel: str) -> Path:
    path = _reject_rel(rel)
    root = base.resolve()
    target = (base / path).resolve(strict=False)
    if not (target == root or root in target.parents):
        raise ToolError("path is not exposed")
    if _is_private(target):
        raise ToolError("path is not exposed")
    if not target.exists():
        raise ToolError(f"not found: {rel}")
    return target


def _is_private(path: Path) -> bool:
    parts = path.resolve(strict=False).parts
    return (
        ("private" in parts and "user" in parts)
        or ("reference" in parts and "people" in parts)
        or "raw" in parts
        or "serve" in parts
    )


def _tracked_paths(repo: Path) -> tuple[str, ...]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--cached", "-z"],
            check=False,
            capture_output=True,
        )
    except OSError:
        return ()
    if result.returncode:
        return ()
    return tuple(
        part.decode(errors="surrogateescape")
        for part in result.stdout.split(b"\0")
        if part
    )


def _is_publicly_classified(path: Path, cfg: BrainMcpConfig) -> bool:
    target = path.resolve(strict=False)
    brain = cfg.brain.resolve()
    if target == brain:
        return True
    try:
        relative = target.relative_to(brain)
    except ValueError:
        return False
    if len(relative.parts) == 1 and relative.name in {
        "AGENTS.md",
        "BOOTSTRAP.md",
        "CONVENTIONS.md",
    }:
        return True
    for repo in (cfg.engine, cfg.agents, cfg.user):
        repo = repo.resolve()
        try:
            relative = target.relative_to(repo)
        except ValueError:
            continue
        tracked = _tracked_paths(repo)
        if target == repo:
            return bool(tracked)
        text = relative.as_posix()
        if target.is_dir():
            prefix = text.rstrip("/") + "/"
            return any(item == text or item.startswith(prefix) for item in tracked)
        return text in tracked
    return False


def _is_nonprivate_excluded(path: Path, cfg: BrainMcpConfig) -> bool:
    try:
        relative = path.resolve(strict=False).relative_to(cfg.brain.resolve())
    except ValueError:
        return True
    parts = relative.parts
    lowered = tuple(part.casefold() for part in parts)
    secret_names = {
        ".netrc",
        ".npmrc",
        "auth.json",
        "auth.lock",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
    secret_component = re.compile(
        r"(?:^|[_-])(?:auth|credential|credentials|secret|secrets|token|tokens)(?:[_\-.]|$)"
    )
    return (
        _is_private(path)
        or ".brain-runtime" in parts
        or ".git" in parts
        or ".ssh" in parts
        or (len(parts) >= 2 and parts[0] == "user" and parts[1] == "private")
        or any(part == ".env" or part.startswith(".env.") for part in lowered)
        or any(part in secret_names for part in lowered)
        or any(secret_component.search(part) for part in lowered)
        or any(part.endswith((".key", ".pem", ".p12", ".pfx")) for part in lowered)
        or not _is_publicly_classified(path, cfg)
    )


def _nonprivate_target(cfg: BrainMcpConfig, rel: str) -> Path:
    relative = _reject_rel(rel)
    candidate = cfg.brain / relative
    current = cfg.brain
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ToolError("path is not exposed")
    target = candidate.resolve(strict=False)
    if _is_nonprivate_excluded(target, cfg):
        raise ToolError("path is not exposed")
    if not target.exists():
        raise ToolError(f"not found: {rel}")
    return target


def read_nonprivate(cfg: BrainMcpConfig, path: str) -> str:
    target = _nonprivate_target(cfg, path)
    if not target.is_file():
        raise ToolError("path is not a file")
    if target.stat().st_size > MAX_TEXT:
        raise ToolError("file too large")
    return target.read_text(errors="replace")


def list_nonprivate(cfg: BrainMcpConfig, path: str = ".") -> str:
    target = _nonprivate_target(cfg, path)
    if not target.is_dir():
        raise ToolError("path is not a directory")
    entries = []
    for child in sorted(target.iterdir(), key=lambda value: value.name):
        if child.is_symlink() or _is_nonprivate_excluded(child, cfg):
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
    return "\n".join(entries) if entries else "(empty)"


def _read(path: Path) -> str:
    if _is_private(path):
        raise ToolError("path is not exposed")
    return path.read_text(errors="replace")


def _banner(path: Path, root: Path) -> str:
    return f"# ===== {path.relative_to(root)} =====\n\n"


def _md_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.md")
        if path.is_file() and not path.is_symlink() and not _is_private(path)
    )


def list_context(cfg: BrainMcpConfig) -> str:
    disciplines = (
        sorted(path.name for path in (cfg.agents / "rules").iterdir() if path.is_dir())
        if (cfg.agents / "rules").exists()
        else []
    )
    skills = (
        sorted(
            path.name
            for path in (cfg.agents / "skills").iterdir()
            if (path / "SKILL.md").is_file()
        )
        if (cfg.agents / "skills").exists()
        else []
    )
    user_context = (
        sorted(path.stem for path in (cfg.user / "profile").glob("*.md"))
        if (cfg.user / "profile").exists()
        else []
    )
    return (
        "# brain-mcp index\n\n"
        "Private paths and reference/people are not exposed.\n\n"
        "## disciplines\n- " + "\n- ".join(disciplines or ["(none)"]) + "\n\n"
        "## skills\n- " + "\n- ".join(skills or ["(none)"]) + "\n\n"
        "## user context\n- " + "\n- ".join(user_context or ["(none)"]) + "\n"
    )


def get_core(cfg: BrainMcpConfig) -> str:
    files = []
    entry = cfg.agents / "README.md"
    if entry.is_file():
        files.append((entry, cfg.agents, "agents"))
    if (cfg.agents / "core").exists():
        files.extend((path, cfg.agents, "agents") for path in _md_files(cfg.agents / "core"))
    entry = cfg.user / "README.md"
    if entry.is_file():
        files.append((entry, cfg.user, "user"))
    return "\n\n".join(
        f"# ===== {store}/{path.relative_to(root)} =====\n\n" + _read(path)
        for path, root, store in files
    )


def get_discipline(cfg: BrainMcpConfig, name: str) -> str:
    directory = _safe_existing(cfg.agents / "rules", name)
    if not directory.is_dir():
        raise ToolError(f"unknown discipline: {name}")
    return "\n\n".join(
        _banner(path, cfg.agents) + _read(path)
        for path in _md_files(directory)
    )


def get_skill(cfg: BrainMcpConfig, name: str) -> str:
    return _read(_safe_existing(cfg.agents / "skills", f"{name}/SKILL.md"))


def get_user_context(cfg: BrainMcpConfig, field: str) -> str:
    path = _reject_rel(field)
    relative = path if len(path.parts) > 1 else Path("profile") / f"{path.name}.md"
    return _read(_safe_existing(cfg.user, str(relative)))


def get_reference(cfg: BrainMcpConfig, path: str) -> str:
    target = _safe_existing(cfg.agents / "reference", path)
    if target.is_dir():
        return "\n".join(
            sorted(child.name for child in target.iterdir() if not child.name.startswith("."))
        )
    return _read(target)


def search(cfg: BrainMcpConfig, query: str) -> str:
    query = str(query or "").strip()
    if len(query) < 2:
        raise ToolError("query too short")
    hits = []
    roots = (
        cfg.agents / "core",
        cfg.agents / "rules",
        cfg.agents / "skills",
        cfg.agents / "reference",
        cfg.user / "profile",
    )
    for root in roots:
        if not root.exists():
            continue
        for path in _md_files(root):
            for line_number, line in enumerate(
                path.read_text(errors="replace").splitlines(),
                1,
            ):
                if query.casefold() in line.casefold():
                    hits.append(
                        f"{path.relative_to(cfg.brain)}:{line_number}: {line[:240]}"
                    )
                    break
            if len(hits) >= 50:
                break
    return "\n".join(hits) if hits else f"No matches for: {query}"
