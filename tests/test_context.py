#!/usr/bin/env python3
"""brain-context manifest and chunking tests."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

from lib import context

root = Path(tempfile.mkdtemp(prefix="brain-context-"))
agents, user = root / "agents", root / "user"
(agents / "core").mkdir(parents=True)
(agents / "skills" / "tone").mkdir(parents=True)
user.mkdir()
(agents / "README.md").write_text(
    "---\nsession:\n  - core/*.md\n  - skills/tone/SKILL.md\n  - missing/*.md\n"
    "subagent:\n  - skills/tone/SKILL.md\n---\n\n# Agents\nENTRY\n"
)
(agents / "core" / "b.md").write_text("B\n")
(agents / "core" / "a.md").write_text("A\n")
(agents / "skills" / "tone" / "SKILL.md").write_text("---\nname: tone\n---\nTONE\n")
(user / "README.md").write_text("USER\n")

manifest, body = context.split_frontmatter((agents / "README.md").read_text())
assert manifest["session"] == ["core/*.md", "skills/tone/SKILL.md", "missing/*.md"]
assert body.startswith("# Agents")

text = "\n".join(context.chunks(agents, user, "session"))
order = ["agents/README.md", "agents/core/a.md", "agents/core/b.md", "agents/skills/tone/SKILL.md", "user/README.md"]
positions = [text.index(f"# ===== {label} =====") for label in order]
assert positions == sorted(positions)
assert "session:" not in text and "name: tone" in text
assert "no file matches `missing/*.md`" in text
sub = context.chunks(agents, user, "subagent")
assert len(sub) == 1 and "TONE" in sub[0] and "ENTRY" not in sub[0] and "USER" not in sub[0]
assert context.chunks(agents, None, "session")[-1].count("user/README.md") == 0

# Chunks stay under budget; overflow is reported, never silently dropped.
for index in range(12):
    (agents / "core" / f"big{index:02d}.md").write_text("x" * 4000 + "\n")
packed = context.chunks(agents, user, "session")
assert len(packed) == context.CHUNKS
assert all(len(chunk) <= context.BUDGET for chunk in packed[:-1])
assert "read these directly" in packed[-1] and "user/README.md" in packed[-1]

try:
    context._matches(agents, "../user/*")
except ValueError:
    pass
else:
    raise AssertionError("escaping pattern accepted")

config = root / "brain.config.toml"
config.write_text(f'[paths]\nagents = "{agents}"\nuser = "{user}"\n')
run = lambda *args: subprocess.run([str(ENGINE / "bin" / "brain-context"), "--config", str(config), *args],
                                   text=True, capture_output=True, check=True).stdout
out = json.loads(run("subagent", "--chunk", "1", "--json", "SubagentStart"))
assert out["hookSpecificOutput"]["hookEventName"] == "SubagentStart"
assert "TONE" in out["hookSpecificOutput"]["additionalContext"]
assert run("subagent", "--chunk", "2", "--json", "SubagentStart") == ""
print("brain-context tests: PASS")
