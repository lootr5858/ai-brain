"""Always-loaded brain context, assembled from the agent store's README manifest.

The agents README frontmatter names context sets as glob lists, for example:

    ---
    session:
      - core/*.md
    subagent:
      - core/working-style.md
    ---

The session set also starts with the agents README and ends with the user README.
A set prints as one hook output of at most BUDGET characters; files that do not
fit are named in a closing note so the agent reads them directly.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

BUDGET = 9000  # Claude Code replaces hook output of about 10k characters with a preview.
NOTE_ROOM = 1000  # kept free for the closing note when one prints
SETS = ("session", "subagent")


def split_frontmatter(text: str) -> tuple[dict[str, list[str]], str]:
    """Parse `key:` lines followed by `  - item` lines; other frontmatter is ignored."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return {}, text
    data: dict[str, list[str]] = {}
    key = None
    for raw in lines[1:end]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw[:1] not in (" ", "\t") and stripped.endswith(":"):
            key = stripped[:-1].strip()
            data[key] = []
        elif key and stripped.startswith("- "):
            data[key].append(stripped[2:].strip().strip("'\""))
        else:
            key = None
    return data, "\n".join(lines[end + 1:]).lstrip("\n")


def _matches(root: Path, pattern: str) -> list[Path]:
    rel = PurePosixPath(pattern)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"manifest pattern must stay inside the store: {pattern}")
    return sorted(path for path in root.glob(pattern) if path.is_file())


def files(agents: Path, user: Path | None, name: str) -> tuple[list[tuple[str, Path]], list[str]]:
    """Return (label, path) pairs for a set, plus notes about unmatched patterns."""
    readme = agents / "README.md"
    manifest, _ = split_frontmatter(readme.read_text()) if readme.is_file() else ({}, "")
    selected: list[tuple[str, Path]] = []
    notes: list[str] = []
    if name == "session" and readme.is_file():
        selected.append(("agents/README.md", readme))
    for pattern in manifest.get(name, []):
        found = _matches(agents, pattern)
        if not found:
            notes.append(f"no file matches `{pattern}` in agents/README.md `{name}`")
        selected.extend((f"agents/{path.relative_to(agents)}", path) for path in found)
    if name == "session" and user is not None and (user / "README.md").is_file():
        selected.append(("user/README.md", user / "README.md"))
    seen: set[Path] = set()
    unique = []
    for label, path in selected:
        if path.resolve() not in seen:
            seen.add(path.resolve())
            unique.append((label, path))
    return unique, notes


def _section(label: str, path: Path) -> str:
    text = path.read_text(errors="replace")
    if path.name == "README.md":
        text = split_frontmatter(text)[1]
    return f"# ===== {label} =====\n\n{text.rstrip()}\n"


def text(agents: Path, user: Path | None, name: str, budget: int = BUDGET) -> str:
    """Return a set's files, in manifest order, as one block of at most `budget` characters."""
    selected, notes = files(agents, user, name)
    loaded = [(label, _section(label, path)) for label, path in selected]
    whole = not notes and sum(len(s) + 1 for _, s in loaded) <= budget
    room = budget if whole else budget - NOTE_ROOM
    sections: list[str] = []
    skipped: list[str] = []
    used = 0
    for label, section in loaded:
        if used + len(section) + 1 <= room:
            sections.append(section)
            used += len(section) + 1
        else:
            skipped.append(label)
    if skipped:
        notes.append(f"over the {budget}-character hook budget; read these directly: "
                     + ", ".join(f"`{label}`" for label in skipped))
    if notes:
        sections.append("# ===== brain hook notes =====\n\n" + "\n".join(f"- {n}" for n in notes) + "\n")
    return "\n".join(sections)
