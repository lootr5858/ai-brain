#!/usr/bin/env python3
"""Encrypted restic backup for brain user data."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from lib.ops import backup_cache_dir


REQUIRED = ("BRAIN_ROOT", "RESTIC_REPOSITORY", "RESTIC_PASSWORD_FILE")
ACK = "encrypted-user-data"


def note(msg: str) -> None:
    print(f"brain-user-backup: {msg}", file=sys.stderr)


def cache_dir(env: dict[str, str]) -> Path:
    return backup_cache_dir(env)


def sentinel(env: dict[str, str]) -> Path:
    return cache_dir(env) / "backup.FAILED"


def marker(env: dict[str, str], name: str) -> Path:
    return cache_dir(env) / name


def write_marker(path: Path) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(datetime.now(timezone.utc).isoformat() + "\n")
    os.replace(temporary, path)


def fail(env: dict[str, str], msg: str) -> int:
    note(msg)
    p = sentinel(env)
    p.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    p.write_text(msg + "\n")
    return 1


def require_env(env: dict[str, str]) -> tuple[Path, Path, Path]:
    if any(not env.get(k) for k in REQUIRED):
        raise ValueError("missing required backup environment")
    if env.get("BRAIN_USER_BACKUP_ACK") != ACK:
        raise ValueError("missing encrypted user-data acknowledgement")
    brain = Path(env["BRAIN_ROOT"]).expanduser().resolve(strict=True)
    user = (brain / "user").resolve(strict=True)
    config = (brain / "brain.config.toml").resolve(strict=True)
    Path(env["RESTIC_PASSWORD_FILE"]).expanduser().resolve(strict=True)
    return brain, user, config


def call(run, cmd: list[str], env: dict[str, str], check: bool = True):
    return run(cmd, check=check, text=True, capture_output=True, env=env)


def ensure_repo(run, env: dict[str, str]) -> None:
    result = call(run, ["restic", "cat", "config"], env=env, check=False)
    if result.returncode == 0:
        return
    if result.returncode == 10:
        call(run, ["restic", "init"], env=env)
        return
    raise RuntimeError("repository probe failed")


def run_backup(env: dict[str, str] | None = None, run=subprocess.run) -> int:
    env = dict(os.environ if env is None else env)
    try:
        _, user, config = require_env(env)
        restic_env = dict(os.environ)
        restic_env.update(env)
        restic_env["RESTIC_PASSWORD_FILE"] = str(
            Path(env["RESTIC_PASSWORD_FILE"]).expanduser().resolve(strict=True)
        )

        ensure_repo(run, restic_env)
        host = socket.gethostname()
        call(
            run,
            [
                "restic",
                "backup",
                str(user),
                str(config),
                "--tag",
                "brain-user",
                "--host",
                host,
            ],
            env=restic_env,
        )
        call(
            run,
            [
                "restic",
                "forget",
                "--tag",
                "brain-user",
                "--host",
                host,
                "--keep-daily",
                "7",
                "--keep-weekly",
                "4",
                "--keep-monthly",
                "6",
                "--prune",
            ],
            env=restic_env,
        )
        call(run, ["restic", "check"], env=restic_env)
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError):
        return fail(env, "backup failed")
    write_marker(marker(env, "backup.OK"))
    sentinel(env).unlink(missing_ok=True)
    return 0


def restore_stage(env: dict[str, str]) -> Path:
    return cache_dir(env) / "restore-stage"


def _restored_paths(env: dict[str, str], stage: Path) -> tuple[Path, Path]:
    brain, _, _ = require_env(env)
    restored_brain = stage / str(brain).lstrip(os.sep)
    return restored_brain / "user", restored_brain / "brain.config.toml"


def _verify_restore(env: dict[str, str], stage: Path) -> dict:
    restored_user, restored_config = _restored_paths(env, stage)
    if stage.is_symlink() or not restored_user.is_dir() or restored_user.is_symlink():
        raise ValueError("invalid staged user data")
    if not restored_config.is_file() or restored_config.is_symlink():
        raise ValueError("invalid staged configuration")

    file_count = 0
    for path in sorted((restored_config, *restored_user.rglob("*")), key=lambda item: str(item)):
        if path.is_symlink():
            raise ValueError("staged restore contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("staged restore contains an unsupported entry")
        with path.open("rb") as handle:
            for _ in iter(lambda: handle.read(1024 * 1024), b""):
                pass
        file_count += 1

    metadata = {
        "verified": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "restored_paths": ["user", "brain.config.toml"],
        "file_count": file_count,
    }
    temporary = stage / ".restore.json.tmp"
    temporary.write_text(json.dumps(metadata, sort_keys=True) + "\n")
    os.replace(temporary, stage / "restore.json")
    write_marker(marker(env, "restore.OK"))
    return metadata


def verify_restore(env: dict[str, str] | None = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        _verify_restore(env, restore_stage(env))
    except (OSError, ValueError):
        note("restore verification failed")
        return 1
    return 0


def stage_restore(env: dict[str, str] | None = None, run=subprocess.run) -> int:
    env = dict(os.environ if env is None else env)
    stage = restore_stage(env)
    try:
        require_env(env)
        if stage.exists() or stage.is_symlink():
            if stage.is_symlink() or not stage.is_dir():
                raise ValueError("unsafe restore stage")
            shutil.rmtree(stage)
        stage.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        stage.mkdir(mode=0o700)
        restic_env = dict(os.environ)
        restic_env.update(env)
        restic_env["RESTIC_PASSWORD_FILE"] = str(
            Path(env["RESTIC_PASSWORD_FILE"]).expanduser().resolve(strict=True)
        )
        call(
            run,
            [
                "restic",
                "restore",
                "latest",
                "--tag",
                "brain-user",
                "--host",
                socket.gethostname(),
                "--target",
                str(stage),
            ],
            env=restic_env,
        )
        _verify_restore(env, stage)
    except (OSError, subprocess.CalledProcessError, ValueError):
        note("restore staging failed")
        return 1
    return 0


def replace_restore(env: dict[str, str] | None = None, confirmed: bool = False) -> int:
    env = dict(os.environ if env is None else env)
    if not confirmed:
        note("replacement confirmation required")
        return 1
    stage = restore_stage(env)
    transaction = None
    user_moved = user_installed = config_moved = config_installed = False
    try:
        brain, user, config = require_env(env)
        _verify_restore(env, stage)
        restored_user, restored_config = _restored_paths(env, stage)
        transaction = Path(tempfile.mkdtemp(prefix=".restore-replacement-", dir=brain))
        incoming_user = transaction / "incoming-user"
        incoming_config = transaction / "incoming-config"
        previous_user = transaction / "previous-user"
        previous_config = transaction / "previous-config"
        shutil.copytree(restored_user, incoming_user, symlinks=False)
        shutil.copy2(restored_config, incoming_config)

        os.replace(user, previous_user)
        user_moved = True
        os.replace(incoming_user, user)
        user_installed = True
        os.replace(config, previous_config)
        config_moved = True
        os.replace(incoming_config, config)
        config_installed = True
        # ponytail: leave a private stale transaction only if cleanup fails; never risk rollback data.
        shutil.rmtree(transaction, ignore_errors=True)
        transaction = None
        shutil.rmtree(stage, ignore_errors=True)
    except (OSError, ValueError):
        try:
            if transaction is not None:
                if config_installed:
                    os.replace(config, transaction / "incoming-config")
                if config_moved:
                    os.replace(transaction / "previous-config", config)
                if user_installed:
                    os.replace(user, transaction / "incoming-user")
                if user_moved:
                    os.replace(transaction / "previous-user", user)
        except OSError:
            pass
        note("restore replacement failed")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", nargs="?", default="run", choices=("run", "stage", "verify", "replace"))
    parser.add_argument("--confirm-replacement", action="store_true")
    args = parser.parse_args()
    root = cache_dir(dict(os.environ))
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(root / "operation.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        # ponytail: one lock serializes backup and replacement; split only if concurrency matters.
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == "run":
            return run_backup()
        if args.action == "stage":
            return stage_restore()
        if args.action == "verify":
            return verify_restore()
        return replace_restore(confirmed=args.confirm_replacement)


if __name__ == "__main__":
    raise SystemExit(main())
