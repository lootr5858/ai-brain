#!/usr/bin/env python3
"""Install Brain skills and plugins. Defaults to a preview without writes or CLI calls."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.config import load_config
from lib.fanout import scan_skill
from lib.fanout_install import execute, make_plan


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform the reviewed changes")
    ap.add_argument("--dry-run", action="store_true", help="explicit preview; overrides --apply")
    ap.add_argument("--uninstall", action="store_true", help="remove only Brain-managed installations")
    ap.add_argument("--config", help="Brain configuration file")
    ap.add_argument("--kind", choices=("skills", "plugins", "all"), default="all")
    ap.add_argument("--target", action="append", help="configured target name; repeatable")
    ap.add_argument("--root", type=Path, help="redirect skills/state/settings under DIR; requires --kind skills")
    ap.add_argument("--settings", type=Path, help="override Claude settings.json destination")
    ap.add_argument("--scan", type=Path, help="scan an arbitrary standalone skill tree and exit")
    return ap


def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)
    if sys.version_info < (3, 11):
        ap.error("engine requires Python 3.11+")
    if args.scan:
        findings = scan_skill(args.scan)
        for finding in findings:
            print(f"FINDING {finding[0]}:{finding[1]} [{finding[2]}] {finding[3]}")
        return int(bool(findings))
    plan = None
    try:
        plan = make_plan(load_config(args.config), targets=args.target, kind=args.kind,
                         uninstall=args.uninstall, root=args.root, settings=args.settings)
        for line in plan.reports:
            print(line)
        if plan.blocked:
            print("REFUSE plan contains conflicts; nothing applied")
            return 1
        if args.apply and not args.dry_run:
            reported = len(plan.reports)
            execute(plan)
            for line in plan.reports[reported:]:
                print(line)
            print(f"[skill/plugin install] completed {len(plan.actions)} managed operation(s)")
        else:
            print(f"[skill/plugin install] {len(plan.actions)} proposed operation(s); dry-run only")
            print("INFO pass --apply to write; native ownership is checked before changes")
        return 0
    except (OSError, ValueError, RuntimeError, AssertionError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        if plan is not None and args.apply and not args.dry_run:
            print(f"Stopped. Earlier operations may have completed; inspect {plan.state.path} "
                  "and rerun a dry-run before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
