#!/usr/bin/env python3
"""Skill fan-out installer. Default is dry-run; writes require --apply."""
import argparse, json, shutil, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.config import load_config
import lib.fanout as fanout

def merge_settings(path):
    s = json.loads(path.read_text()) if path.exists() else {}
    if s.get("skillListingBudgetFraction") == 0.025:
        return False
    s["skillListingBudgetFraction"] = 0.025
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=2) + "\n")
    tmp.replace(path)
    return True

def place(name, src, tgt_dir, mode, apply):
    dest = tgt_dir / name
    verb = "COPY" if mode == "copy" else "LINK"
    if apply:
        if dest.is_symlink():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)
        if mode == "copy":
            shutil.copytree(src, dest)
            (dest / ".brain-managed").touch()
        else:
            dest.symlink_to(src.resolve())
    return f"{verb} {tgt_dir.name} {name} -> {src}"

def remove_managed(name, src, tgt_dir, apply):
    dest = tgt_dir / name
    if not dest.exists() and not dest.is_symlink():
        return None
    if fanout.target_conflict(name, src, tgt_dir) is not None:
        return f"REFUSE {tgt_dir.name} {name}: unmanaged target, not removing"
    if apply:
        if dest.is_symlink():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)
    return f"REMOVE {tgt_dir.name} {name}"


def unsafe_skills(skills):
    unsafe = {}
    for name, directory in skills.items():
        problems = list(fanout.validate_skill(directory))
        problems.extend(
            f"scan {finding[0]}:{finding[1]} [{finding[2]}] {finding[3]}"
            for finding in fanout.scan_skill(directory)
        )
        if problems:
            unsafe[name] = problems
    return unsafe

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform writes; default only reports")
    ap.add_argument("--dry-run", action="store_true", help="accepted for explicit no-write mode")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--config")
    ap.add_argument("--root", type=Path, help="remap targets under DIR")
    ap.add_argument("--settings", type=Path, default=Path.home() / ".claude" / "settings.json")
    ap.add_argument("--target", action="append", help="fan out only to this configured target; repeatable")
    ap.add_argument("--scan", type=Path, help="scan an arbitrary skill tree and exit")
    a = ap.parse_args()
    assert sys.version_info >= (3, 11), "engine requires python3.11+"

    if a.scan:
        finds = fanout.scan_skill(a.scan)
        for f in finds:
            print(f"FINDING {f[0]}:{f[1]} [{f[2]}] {f[3]}")
        return 1 if finds else 0

    cfg = load_config(a.config)
    fan = cfg["engine"]["fanout"]
    mode = fan.get("mode", "symlink")
    apply = a.apply and not a.dry_run
    targets = {n: (a.root / n if a.root else Path(p).expanduser()) for n, p in fan["targets"].items()}
    if a.target:
        unknown = sorted(set(a.target) - set(targets))
        if unknown:
            ap.error(f"unknown target(s): {', '.join(unknown)}")
        targets = {n: targets[n] for n in targets if n in set(a.target)}
    skills = fanout.load_skills(cfg)
    report = []

    if a.uninstall:
        for _, tdir in targets.items():
            for name, src in skills.items():
                line = remove_managed(name, src, tdir, apply)
                if line:
                    report.append(line)
        print("\n".join(report) or "nothing managed to remove")
        return 1 if any(l.startswith("REFUSE") for l in report) else 0

    unsafe = unsafe_skills(skills)
    for name, problems in unsafe.items():
        for problem in problems:
            report.append(f"REFUSE {name}: {problem}")

    actions = []
    for tname, tdir in targets.items():
        for name, src in sorted(skills.items()):
            if name in unsafe:
                continue
            conflict = fanout.target_conflict(name, src, tdir)
            if conflict:
                report.append(f"REFUSE {tname} {name}: {conflict}")
            else:
                actions.append((name, src, tdir, mode))

    if apply:
        for _, _, tdir, _ in actions:
            tdir.mkdir(parents=True, exist_ok=True)
        for action in actions:
            report.append(place(*action, apply=True))
        if "claude-code" in targets and merge_settings(a.settings):
            report.append(f"INFO skillListingBudgetFraction=0.025 -> {a.settings}")
    else:
        for action in actions:
            report.append(place(*action, apply=False))
        report.append("INFO dry-run only; pass --apply to write")

    print("\n".join(report))
    bad = [l for l in report if l.startswith("REFUSE")]
    print(f"[skill-fanout install] {len(actions)} placement(s), "
          f"{len(bad)} conflict(s){'' if apply else ' (dry-run)'}")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
