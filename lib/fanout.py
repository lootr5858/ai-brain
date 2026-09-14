#!/usr/bin/env python3
"""Skill fan-out library - pure functions over agents/skills + runtime targets."""
import os
import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
SUPPORT_DIRS = {"references", "templates", "assets", "scripts"}

def skills_root(cfg):
    return Path(cfg["paths"]["agents"]) / "skills"

def load_skills(cfg):
    root = skills_root(cfg)
    if not root.exists():
        return {}
    return {d.name: d for d in sorted(root.iterdir())
            if not d.name.startswith(".") and d.is_dir()
            and (d / "SKILL.md").is_file()
            and not any((d / manifest).exists() for manifest in
                        (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"))}

def parse_frontmatter(path):
    lines = path.read_text().split("\n")
    assert lines[0] == "---", f"{path}: no frontmatter"
    end = lines.index("---", 1)
    fm, key = {}, None
    for raw in lines[1:end]:
        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", raw)
        if m:
            key = m.group(1)
            fm[key] = m.group(2).strip().strip('"')
        elif key and raw[:1] in (" ", "\t"):
            fm[key] = (fm[key] + " " + raw.strip()).strip()
        elif raw.strip():
            raise ValueError(f"{path}: unrecognized frontmatter line {raw!r}")
    return fm, lines[end + 1:]

def validate_skill(d):
    smd = d / "SKILL.md"
    if not smd.is_file():
        return [f"{d.name}: no SKILL.md"]
    try:
        fm, body = parse_frontmatter(smd)
    except (ValueError, AssertionError) as e:
        return [f"{d.name}: {e}"]
    probs = []
    if fm.get("name") != d.name:
        probs.append(f"{d.name}: frontmatter name {fm.get('name')!r} != dir")
    if not fm.get("description"):
        probs.append(f"{d.name}: empty description")
    if fm.get("description") and len(fm["description"]) > 1024:
        probs.append(f"{d.name}: description >1024 chars")
    bad = set(fm) - SPEC_KEYS
    if bad:
        probs.append(f"{d.name}: non-spec keys {sorted(bad)}")
    for ref in re.findall(r"\]\(([^)#][^)]*)\)", "\n".join(body)):
        path_like = ref.startswith(("./", "../")) or "/" in ref or re.search(r"\.[A-Za-z0-9]+$", ref)
        if path_like and not ref.startswith(("http://", "https://", "mailto:")) and not (d / ref).exists():
            probs.append(f"{d.name}: broken relative resource {ref}")
    return probs

SCAN_RULES = [
    ("base64-blob",      re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")),
    ("unicode-obfusc",   re.compile(r"[\u202A-\u202E\u2066-\u2069\U000e0000-\U000e007f]")),
    ("download-exec",    re.compile(r"(?i)\b(curl|wget)\b[^\n|]*\|\s*(ba|z|da)?sh\b|\biwr\b[^\n]*\|\s*iex\b")),
    ("reverse-shell",    re.compile(r"/dev/(tcp|udp)/|\bnc\b[^\n]{0,40}\s-e\s")),
    ("cred-path-read",   re.compile(r"(?i)\b(cat|cp|scp|read|open|curl|tar|zip|upload|send|paste)\b"
                                    r"[^\n]*(~|\$HOME)/\.(ssh|aws|gnupg|config/gh)\b")),
    ("env-secret-exfil", re.compile(r"\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD)\w*\}?[^\n]{0,80}https?://")),
    ("persistence",      re.compile(r"(?i)systemctl\s+(enable|edit)\b|>>\s*[^\n]*\.(bashrc|zshrc|profile)\b"
                                    r"|crontab\s+-")),
    ("safety-disable",   re.compile(r"(?i)--dangerously|--no-verify\b"
                                    r"|disable[^\n]{0,24}(sandbox|review|verification|safety)")),
    ("prompt-injection", re.compile(r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)"
                                    r"|do not (tell|inform|mention)[^\n]{0,24}user"
                                    r"|without (asking|telling|informing) the user")),
    ("control-plane",    re.compile(r"(?i)\b(edit|append[^\n]{0,32}to|write[^\n]{0,32}to|modify|update)\b[^\n]{0,50}"
                                    r"\b(CLAUDE\.md|AGENTS\.md|MEMORY\.md|SOUL\.md|settings(\.local)?\.json"
                                    r"|hooks\.json|\.mcp\.json)")),
    ("dynamic-exec",     re.compile(r"(?i)\beval\s*\(|\bexec\s*\(|shell\s*=\s*True"
                                    r"|base64\s+(-d|--decode)[^\n]*\|")),
    ("hardcoded-secret", re.compile(r"(?i)(api[_-]?key|secret|token|passwd|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}"
                                    r"|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,}")),
]

def scan_skill(d):
    finds = []
    files = []
    for directory, dirs, names in os.walk(d, followlinks=False):
        # Git history/index files are repository metadata, not skill resources.
        # Prune before traversal; .git may also be a worktree pointer file.
        dirs[:] = [name for name in dirs if name != ".git"]
        files.extend(Path(directory) / name for name in names
                     if name != ".git" and (Path(directory) / name).is_file())
    for f in sorted(files):
        if f.stat().st_size > 1_000_000:
            finds.append((str(f.relative_to(d)), 0, "oversize-file", f"{f.stat().st_size} bytes"))
            continue
        try:
            text = f.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            finds.append((str(f.relative_to(d)), 0, "non-text-file", "binary/undecodable content"))
            continue
        for cls, rx in SCAN_RULES:
            m = rx.search(text)
            if m:
                finds.append((str(f.relative_to(d)), text[:m.start()].count("\n") + 1, cls,
                              m.group(0)[:80]))
    return finds

def target_conflict(name, canonical_dir, target_dir):
    dest = target_dir / name
    if not dest.exists() and not dest.is_symlink():
        return None
    if dest.is_symlink():
        try:
            if dest.resolve() == canonical_dir.resolve():
                return None
        except OSError:
            pass
    elif dest.is_dir() and (dest / ".brain-managed").is_file():
        return None
    return f"{dest} exists and is unmanaged — not clobbering (resolve manually)"
