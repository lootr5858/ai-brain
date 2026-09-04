#!/usr/bin/env python3
"""Backup-assist status and manifest helpers."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.ops import backup_manifest, backup_status, dumps, load_ops_config


def status(cfg) -> None:
    manifest = backup_manifest(cfg)
    state = backup_status(cfg, manifest)
    print("backup-assist status")
    print(f"personal sync target: {'configured' if state.personal_sync_configured else 'not configured'}")
    print(f"dirty repos: {', '.join(state.dirty_repos) if state.dirty_repos else 'none'}")
    for name, repo in manifest.repos.items():
        print(f"{name}: {repo['head'][:12] or 'no-head'} remotes={','.join(repo['remote_names']) or 'none'}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("status", "manifest"))
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config")
    source.add_argument("--root", type=Path)
    args = parser.parse_args()
    selected = args.config or (args.root.resolve() / "brain.config.toml" if args.root else None)
    cfg = load_ops_config(selected)
    if args.command == "status":
        status(cfg)
    else:
        print(dumps(backup_manifest(cfg).as_dict()), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
