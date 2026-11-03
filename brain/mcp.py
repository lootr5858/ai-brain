"""The brain as MCP tools: core rules and skills from the agent store, knowledge from the user store."""
from __future__ import annotations

import functools
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from brain import context
from brain.store import MAX_HITS, Store, StoreError

SKILLS = "skills"
SKILL_FILE = "SKILL.md"
AGENTS, USER = "agents", "user"  # the labels get_core puts on each store's files
INSTRUCTIONS = (
    "This server holds the owner's brain: how the owner wants agents to behave, reusable skills, and the "
    "owner's personal knowledge. Call get_core first in every conversation and follow what it returns. "
    "Find workflows with list_skills and read_skill. Look up the owner's facts with list, read, and search. "
    "edit and create change the owner's files; each change is one git commit the owner can revert. "
    "Paths in get_core's headers work as given: user/... for knowledge, agents/... for the rule files it "
    "points to, read-only."
)
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)


def skill_description(text: str) -> str:
    """Read `description` from SKILL.md frontmatter, folding wrapped and block-scalar values."""
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return ""
    parts: list[str] = []
    collecting = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if collecting and line[:1] in (" ", "\t"):
            parts.append(line.strip())
            continue
        collecting = False
        found = re.match(r"description:\s*(.*)$", line)
        if found:
            collecting = True
            parts.append(found.group(1).strip())
    value = " ".join(part for part in parts if part not in (">", "|", ">-", "|-"))
    return value.strip().strip("'\"")


def skill_names(agents: Store) -> list[str]:
    """A skill is a visible child of the skills folder with a SKILL.md."""
    folder = agents.root / SKILLS
    if not folder.is_dir():
        return []
    return sorted(c.name for c in folder.iterdir() if not c.name.startswith(".") and (c / SKILL_FILE).is_file())


def _refusals_shown(fn):
    """The SDK hides unexpected errors from the model; a store refusal is one it should read."""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StoreError as exc:
            raise ToolError(str(exc)) from None
    return wrapped


def _via() -> str:
    token = get_access_token()
    return f"via {urlsplit(token.client_id).hostname}" if token else ""


def server(brain: Path, **settings: Any) -> MCPServer:
    """Build the MCP server; `settings` pass through to MCPServer (auth for the remote app)."""
    agents_root, user_root = Path(brain) / "agents", Path(brain) / "user"
    agents, user = Store(agents_root), Store(user_root)
    mcp = MCPServer("brain", instructions=INSTRUCTIONS, **settings)

    def tool(name: str, description: str, annotations: ToolAnnotations = READ):
        register = mcp.tool(name=name, description=description, annotations=annotations, structured_output=False)
        return lambda fn: register(_refusals_shown(fn))

    def knowledge(path: str) -> str:
        """A user-store path; `user/...` as get_core shows it works too, unless the store has its own `user`."""
        head, _, rest = path.partition("/")
        return rest if head == USER and not (user.root / USER).exists() else path

    def rule(path: str) -> str | None:
        """The agent-store path behind `agents/...`, unless the user store has its own `agents`."""
        head, _, rest = path.partition("/")
        return rest if head == AGENTS and rest and not (user.root / AGENTS).exists() else None

    def writable(path: str) -> str:
        if rule(path) is not None:
            raise StoreError("the agent rules are read-only here; edit and create change knowledge files only")
        return knowledge(path)

    @tool("get_core", "Call this first in every conversation. Returns the owner's always-loaded agent rules "
                      "and the guide to their personal knowledge; follow them.")
    def get_core() -> str:
        return context.text(agents_root, user_root if user_root.is_dir() else None, "session", budget=sys.maxsize)

    @tool("list_skills", "List the owner's skills as `name: description`. Check it before multi-step work; "
                         "load a matching skill with read_skill.")
    def list_skills() -> str:
        rows = []
        for name in skill_names(agents):
            try:
                rows.append(f"{name}: {skill_description(agents.read(f'{SKILLS}/{name}/{SKILL_FILE}'))}")
            except StoreError:
                continue
        return "\n".join(rows) or "No skills."

    @tool("read_skill", "Read a skill's SKILL.md, which also lists its supporting files, or pass `path` to read "
                        "one of those files. Follow the instructions it gives.")
    def read_skill(name: str, path: str = SKILL_FILE) -> str:
        if name not in skill_names(agents):
            raise StoreError(f"no skill named {name!r}; see list_skills")
        base = f"{SKILLS}/{name}"
        text = agents.read(f"{base}/{path}")
        if path != SKILL_FILE:
            return text
        others = [rel.removeprefix(f"{base}/") for rel, _ in agents.list(base)]
        others = [rel for rel in others if rel != SKILL_FILE]
        if not others:
            return text
        return text.rstrip() + "\n\n---\nSupporting files (read_skill with `path`):\n" + "\n".join(f"- {o}" for o in others)

    @tool("list", "List the owner's knowledge files with sizes, recursively. `path` narrows it to one folder.")
    def list_files(path: str = "") -> str:
        return "\n".join(f"{rel} ({size} bytes)" for rel, size in user.list(knowledge(path))) or "No files."

    @tool("read", "Read a file by the path list, search, or the rules give: a knowledge file (`profile.md` or "
                  "`user/profile.md`), or a file the always-loaded rules point to (`agents/governance/mutation.md`, "
                  "read-only).")
    def read(path: str) -> str:
        rel = rule(path)
        if rel is not None:
            return agents.read(rel)
        try:
            return user.read(knowledge(path))
        except StoreError as exc:
            try:
                agents.read(path)
            except StoreError:
                raise exc from None
            raise StoreError(f"not found: {path}; the agent rules have it: read `{AGENTS}/{path}`") from None

    @tool("search", "Search the owner's knowledge files for text, case-insensitive. Returns `path:line: text`.")
    def search(query: str) -> str:
        hits = user.search(query)
        note = [f"Stopped at {MAX_HITS} matches; narrow the query."] if len(hits) == MAX_HITS else []
        return "\n".join(hits + note) or "No matches."

    @tool("edit", "Change one of the owner's knowledge files: replace the exact text `old`, which must occur once, "
                  "with `new`. Read the file first. Saved as one git commit.", WRITE)
    def edit(path: str, old: str, new: str) -> str:
        return f"Saved {path} as commit {user.edit(writable(path), old, new, _via())}."

    @tool("create", "Create a new knowledge file in an existing folder. Fails if it exists. Saved as one git "
                    "commit. Check the knowledge guide from get_core before adding a file.", WRITE)
    def create(path: str, content: str) -> str:
        return f"Created {path} as commit {user.create(writable(path), content, _via())}."

    return mcp
