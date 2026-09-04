#!/usr/bin/env python3
"""Local ops helpers: dashboard status, proposal counts, and backup manifests."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import ctypes
import errno
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import subprocess

from lib.config import load_config


ENGINE = Path(__file__).resolve().parents[1]
BACKUP_RUNNER = ENGINE / "backup" / "brain" / "user" / "backup.py"
BACKUP_ENV = Path.home() / ".config" / "brain-os" / "brain-user-backup.env"
BACKUP_HEALTH_HOURS = 36
SECRET_PARTS = ("secret", "token", "password", "passwd", "private_" + "key", "client_" + "secret", "seed")
USER_WRITE_DIRS = ("profile", "journal", "knowledge", "inventory")
DASHBOARD_USER_WRITE_DIRS = USER_WRITE_DIRS + ("private", "projects")
DASHBOARD_USER_WRITE_ROOTS = ("preferences.md", "context-routing.md")
MANAGED_SERVICES = {
    "dashboard": {
        "label": "Dashboard",
        "unit": "brain-dashboard.service",
        "actions": (),
    },
    "brain_mcp": {
        "label": "Remote brain-mcp",
        "unit": "brain-mcp-remote.service",
        "actions": ("start", "stop", "restart"),
    },
    "backup": {
        "label": "Backup schedule",
        "unit": "brain-user-backup.timer",
        "actions": ("start", "stop", "restart"),
    },
}
SYSTEMD_PROPERTIES = (
    "LoadState",
    "UnitFileState",
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainStatus",
    "StateChangeTimestamp",
)


@dataclass(frozen=True)
class OpsConfig:
    brain: Path
    engine: Path
    agents: Path
    user: Path
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765
    dashboard_public_origin: str = ""
    dashboard_embedding_url: str = ""
    dashboard_embedding_model: str = ""
    notify_command: str = ""
    personal_sync_target: str = ""


@dataclass(frozen=True)
class ProposalBucket:
    agent: str
    count: int
    oldest_mtime: float | None

    def as_dict(self):
        return {"agent": self.agent, "count": self.count, "oldest_mtime": self.oldest_mtime}


@dataclass(frozen=True)
class BackupManifest:
    repos: dict
    user_categories: dict
    rejected_secret_paths: int

    def as_dict(self):
        return {
            "repos": self.repos,
            "user_categories": self.user_categories,
            "rejected_secret_paths": self.rejected_secret_paths,
        }


@dataclass(frozen=True)
class BackupStatus:
    personal_sync_configured: bool
    dirty_repos: list[str]

    def as_dict(self):
        return {
            "personal_sync_configured": self.personal_sync_configured,
            "dirty_repos": self.dirty_repos,
        }


@dataclass(frozen=True)
class WriteResult:
    allowed: bool
    reason: str
    path: str = ""

    def as_dict(self):
        return {"allowed": self.allowed, "reason": self.reason, "path": self.path}


def load_ops_config(config_path: str | Path | None = None) -> OpsConfig:
    data = load_config(config_path)
    paths = data["paths"]
    ops = data.get("engine", {}).get("ops", {})
    dashboard = ops.get("dashboard", {})
    nudge = ops.get("nudge", {})
    backup = ops.get("backup", {})
    return OpsConfig(
        brain=Path(paths["brain"]),
        engine=Path(paths["engine"]),
        agents=Path(paths["agents"]),
        user=Path(paths["user"]),
        dashboard_host=dashboard.get("host", "127.0.0.1"),
        dashboard_port=int(dashboard.get("port", 8765)),
        dashboard_public_origin=str(dashboard.get("public_origin", "")),
        dashboard_embedding_url=str(dashboard.get("embedding_url", "")),
        dashboard_embedding_model=str(dashboard.get("embedding_model", "")),
        notify_command=nudge.get("notify_command", ""),
        personal_sync_target=backup.get("personal_sync_target", ""),
    )


def proposal_summary(agents_root: str | Path) -> list[ProposalBucket]:
    buckets = []
    for directory in sorted((Path(agents_root) / "homes").glob("*/proposals")):
        files = [path for path in directory.rglob("*") if path.is_file() and path.name != ".gitkeep"]
        oldest = min(path.stat().st_mtime for path in files) if files else None
        buckets.append(ProposalBucket(directory.parent.name, len(files), oldest))
    return buckets


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _repo_state(repo: Path) -> dict:
    try:
        remotes = _git(repo, "remote").splitlines()
        dirty_paths = [
            line[3:]
            for line in _git(repo, "status", "--porcelain").splitlines()
            if len(line) > 3
        ]
        return {
            "head": _git(repo, "rev-parse", "HEAD"),
            "dirty": bool(dirty_paths),
            "dirty_paths": dirty_paths,
            "remote_names": remotes,
        }
    except (OSError, subprocess.CalledProcessError):
        return {"head": "", "dirty": True, "dirty_paths": [], "remote_names": []}


def service_status(service: str, observed_time: str | None = None, run=None) -> dict:
    target = MANAGED_SERVICES.get(service)
    if target is None:
        raise ValueError("unknown service")
    observed_time = observed_time or datetime.now(timezone.utc).isoformat()
    runner = run or subprocess.run
    try:
        completed = runner(
            [
                "systemctl",
                "--user",
                "show",
                target["unit"],
                f"--property={','.join(SYSTEMD_PROPERTIES)}",
                "--no-pager",
            ],
            text=True,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None

    fields = {}
    if completed is not None:
        fields = dict(
            line.split("=", 1)
            for line in completed.stdout.splitlines()
            if "=" in line
        )
    installed = fields.get("LoadState") == "loaded"
    enabled = fields.get("UnitFileState") in {"enabled", "enabled-runtime"}
    active = fields.get("ActiveState") == "active"
    result = fields.get("Result", "")
    exit_status = fields.get("ExecMainStatus", "")
    last_failure = result if result and result != "success" else None
    if not last_failure and exit_status not in {"", "0"}:
        last_failure = f"exit status {exit_status}"

    if completed is None or completed.returncode != 0:
        health, reason = "unknown", "systemd status unavailable"
    elif not installed:
        health, reason = "unhealthy", "not installed"
    elif last_failure:
        health, reason = "unhealthy", f"last result: {last_failure}"
    elif not active:
        health, reason = "unhealthy", fields.get("ActiveState") or "inactive"
    elif not enabled:
        health, reason = "unhealthy", "active but not enabled"
    else:
        health = "healthy"
        reason = "/".join(filter(None, (fields.get("ActiveState"), fields.get("SubState"))))

    return {
        "subsystem": f"service:{service}",
        "status": health,
        "observed_time": observed_time,
        "reason": reason,
        "id": service,
        "label": target["label"],
        "installed": installed,
        "enabled": enabled,
        "active": active,
        "last_failure": last_failure,
        "state_changed": fields.get("StateChangeTimestamp", ""),
        "actions": list(target["actions"]),
    }


def service_action(service: str, action: str, run=None) -> dict:
    target = MANAGED_SERVICES.get(service)
    if target is None or action not in target["actions"]:
        raise ValueError("unsupported service action")
    runner = run or subprocess.run
    before = service_status(service, run=runner)
    try:
        completed = runner(
            ["systemctl", "--user", action, target["unit"]],
            text=True,
            capture_output=True,
            timeout=30,
        )
        command_ok = completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        command_ok = False
    return {
        "service": service,
        "action": action,
        "command_ok": command_ok,
        "before": before,
        "after": service_status(service, run=runner),
    }


def _brain_mcp_client(client) -> dict:
    return {
        "name": client.unique_name,
        "profile": client.profile,
        "created_at": datetime.fromtimestamp(
            client.created_at,
            tz=timezone.utc,
        ).date().isoformat(),
    }


def brain_mcp_status(store=None) -> dict:
    from lib.brain_mcp_remote_store import RemoteStore, StoreError, remote_state_path

    observed = datetime.now(timezone.utc).isoformat()
    remote = store
    error = ""
    try:
        if remote is None and remote_state_path().is_file():
            remote = RemoteStore()
        resource = remote.get_canonical_resource() if remote else None
        clients = [_brain_mcp_client(client) for client in remote.list_clients()] if remote else []
    except (OSError, sqlite3.Error, StoreError):
        resource, clients, error = None, [], "remote state is unavailable"
    return {
        "subsystem": "brain-mcp",
        "observed_time": observed,
        "error": error,
        "remote": {
            "configured": resource is not None,
            "resource": resource or "",
            "clients": clients,
        },
    }


def brain_mcp_action(
    action: str,
    *,
    name: str = "",
    profile: str = "",
    redirect_uri: str | None = None,
    confirmed: bool = False,
    store=None,
) -> dict:
    if action not in {"add-client", "revoke-client"}:
        raise ValueError("unsupported brain-mcp action")

    from lib.brain_mcp_remote import RemoteAccessError, client_redirect_uri
    from lib.brain_mcp_remote_store import RemoteStore, StoreError

    remote = store or RemoteStore()
    try:
        if action == "add-client":
            resource = remote.get_canonical_resource()
            if resource is None:
                raise StoreError("run remote init before adding a client")
            created = remote.create_client(
                unique_name=name,
                profile=profile,
                redirect_uri=client_redirect_uri(profile, redirect_uri or None),
            )
            return {
                "action": action,
                "resource": resource,
                "name": created.client.unique_name,
                "profile": created.client.profile,
                "client_id": created.client.client_id,
                "client_secret": created.client_secret,
            }

        if confirmed is not True:
            raise ValueError("client revocation confirmation required")
        client = next(
            (
                client
                for client in remote.list_clients()
                if client.unique_name.casefold() == name.strip().casefold()
            ),
            None,
        )
        if client is None:
            raise ValueError("unknown remote client")
        removed = remote.remove_client(client.client_id)
        if removed is None:
            raise ValueError("unknown remote client")
        return {"action": action, "name": removed}
    except (RemoteAccessError, StoreError) as exc:
        raise ValueError(str(exc)) from None


def system_status(config: OpsConfig, run=None) -> dict:
    observed = datetime.now(timezone.utc).isoformat()
    repositories = []
    for name, path in {"engine": config.engine, "agents": config.agents, "user": config.user}.items():
        state = _repo_state(path)
        state.update({
            "subsystem": f"repository:{name}",
            "status": "healthy" if state["head"] else "unknown",
            "observed_time": observed,
            "reason": f"{len(state['dirty_paths'])} dirty path(s)" if state["dirty"] else "clean",
            "name": name,
        })
        repositories.append(state)

    proposals = [bucket.as_dict() for bucket in proposal_summary(config.agents)]
    pending = sum(bucket["count"] for bucket in proposals)
    return {
        "observed_time": observed,
        "repositories": repositories,
        "proposals": {
            "subsystem": "proposals",
            "status": "healthy",
            "observed_time": observed,
            "reason": f"{pending} pending",
            "buckets": proposals,
        },
        "services": [
            service_status(service, observed, run)
            for service in MANAGED_SERVICES
        ],
    }


def _secretish(path: Path) -> bool:
    return any(any(part in component.lower() for part in SECRET_PARTS) for component in path.parts)


def backup_manifest(cfg: OpsConfig) -> BackupManifest:
    repos = {
        name: _repo_state(path)
        for name, path in {"engine": cfg.engine, "agents": cfg.agents, "user": cfg.user}.items()
    }
    categories: dict[str, object] = {"preferences": (cfg.user / "preferences.md").exists()}
    rejected = 0
    for name in USER_WRITE_DIRS:
        root = cfg.user / name
        count = 0
        if root.exists():
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if _secretish(path.relative_to(cfg.user)):
                    rejected += 1
                    continue
                count += 1
        categories[name] = count
    categories["private"] = "excluded" if (cfg.user / "private").exists() else "absent"
    return BackupManifest(repos, categories, rejected)


def backup_status(config: OpsConfig, manifest: BackupManifest) -> BackupStatus:
    dirty = [name for name, state in manifest.repos.items() if state.get("dirty")]
    return BackupStatus(bool(config.personal_sync_target), dirty)


def backup_cache_dir(env: dict[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    if values.get("XDG_CACHE_HOME"):
        return Path(values["XDG_CACHE_HOME"]) / "brain-user-backup"
    return Path(values.get("HOME", str(Path.home()))) / ".cache" / "brain-user-backup"


def _marker_time(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return ""


def backup_health(
    config: OpsConfig,
    now: datetime | None = None,
    cache: str | Path | None = None,
) -> dict:
    observed = now or datetime.now(timezone.utc)
    root = Path(cache) if cache is not None else backup_cache_dir()
    success_path = root / "backup.OK"
    failure_path = root / "backup.FAILED"
    restore_path = root / "restore.OK"
    last_success = _marker_time(success_path)
    last_failure = _marker_time(failure_path)

    if not last_success:
        status = "unhealthy" if last_failure else "unknown"
        reason = "last backup failed" if last_failure else "no successful backup recorded"
    else:
        success_time = datetime.fromisoformat(last_success)
        age_hours = (observed - success_time).total_seconds() / 3600
        if age_hours < 0:
            status, reason = "unknown", "last success time is in the future"
        elif last_failure and datetime.fromisoformat(last_failure) > success_time:
            status, reason = "unhealthy", "last backup failed"
        elif age_hours > BACKUP_HEALTH_HOURS:
            status, reason = "unhealthy", "last successful backup and check are stale"
        else:
            status, reason = "healthy", "backup and repository check succeeded within 36 hours"

    stage_root = root / "restore-stage"
    stage_present = stage_root.is_dir() and not stage_root.is_symlink()
    metadata_path = stage_root / "restore.json"
    metadata = {}
    try:
        if stage_present and metadata_path.is_file() and not metadata_path.is_symlink():
            metadata = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "subsystem": "backup",
        "status": status,
        "observed_time": observed.isoformat(),
        "reason": reason,
        "last_success": last_success,
        "last_failure": last_failure,
        "manual_restore_test": _marker_time(restore_path),
        "stage": {
            "present": stage_present,
            "verified": stage_present and metadata.get("verified") is True,
            "verified_at": metadata.get("verified_at", "") if stage_present else "",
            "restored_paths": metadata.get("restored_paths", []) if stage_present else [],
            "file_count": metadata.get("file_count", 0) if stage_present else 0,
        },
    }


def backup_action(
    config: OpsConfig,
    action: str,
    confirmed: bool = False,
    run=None,
) -> dict:
    if action not in {"run", "stage", "verify", "replace"}:
        raise ValueError("unsupported backup action")
    if action == "replace" and confirmed is not True:
        raise ValueError("replacement confirmation required")
    if action == "run":
        command = ["systemctl", "--user", "start", "brain-user-backup.service"]
    else:
        command = [
            "systemd-run",
            "--user",
            "--wait",
            "--pipe",
            "--collect",
            "--quiet",
            "--unit=brain-user-backup-control",
            "--service-type=oneshot",
            f"--setenv=PATH={Path.home()}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            f"--property=EnvironmentFile={BACKUP_ENV}",
            "--property=UMask=0077",
            "/usr/bin/python3",
            str(BACKUP_RUNNER),
            action,
        ]
        if action == "replace":
            command.append("--confirm-replacement")
    runner = run or subprocess.run
    try:
        completed = runner(command, text=True, capture_output=True, timeout=3600)
        command_ok = completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        command_ok = False
    return {
        "action": action,
        "command_ok": command_ok,
        "status": backup_health(config),
    }


class _UnsafeWritePath(Exception):
    pass


def _ops_user_relative(user: Path, target: Path) -> Path | None:
    lexical = Path(os.path.abspath(target))
    try:
        relative = lexical.relative_to(user)
    except ValueError:
        return None
    if relative.as_posix() in DASHBOARD_USER_WRITE_ROOTS:
        return relative
    if len(relative.parts) >= 2 and relative.parts[0] in DASHBOARD_USER_WRITE_DIRS:
        return relative
    return None


def content_digest(path: str | Path) -> str:
    target = Path(path)
    if target.is_symlink():
        raise ValueError("digest target must be a regular non-symlink file")
    if not target.exists():
        return ""
    if not target.is_file():
        raise ValueError("digest target must be a regular non-symlink file")
    return hashlib.sha256(target.read_bytes()).hexdigest()


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_parent(user: Path, relative: Path, *, create: bool = True) -> int:
    descriptor = os.open(user, _directory_flags())
    try:
        for component in relative.parent.parts:
            try:
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(component, _directory_flags(), dir_fd=descriptor)
            except OSError as exc:
                raise _UnsafeWritePath("refusing symlink or non-directory path component") from exc
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _digest_at(directory_fd: int, name: str) -> str:
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise _UnsafeWritePath("refusing symlink target") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _UnsafeWritePath("target is not a regular file")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def user_write(
    target: str | Path,
    content: str,
    user_root: str | Path,
    expected_digest: str,
) -> WriteResult:
    user = Path(user_root).resolve()
    target = Path(target)
    if not target.is_absolute():
        target = user / target
    relative = _ops_user_relative(user, target)
    if relative is None:
        return WriteResult(False, "not an ops-writable user path", str(target))

    parent_fd = None
    temporary = f".{relative.name}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
    try:
        parent_fd = _open_parent(user, relative)
        if _digest_at(parent_fd, relative.name) != expected_digest:
            return WriteResult(False, "stale content digest", str(target))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o666, dir_fd=parent_fd)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if _digest_at(parent_fd, relative.name) != expected_digest:
            return WriteResult(False, "stale content digest", str(target))
        os.replace(
            temporary,
            relative.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except _UnsafeWritePath as exc:
        return WriteResult(False, str(exc), str(target))
    except OSError as exc:
        return WriteResult(False, f"write failed: {exc.strerror or type(exc).__name__}", str(target))
    finally:
        if parent_fd is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
    return WriteResult(True, "written", str(target))


def user_move(
    source: str | Path,
    destination: str | Path,
    user_root: str | Path,
    expected_digest: str,
) -> WriteResult:
    user = Path(user_root).resolve()
    source = Path(source) if Path(source).is_absolute() else user / source
    destination = Path(destination) if Path(destination).is_absolute() else user / destination
    source_relative = _ops_user_relative(user, source)
    destination_relative = _ops_user_relative(user, destination)
    if source_relative is None or destination_relative is None:
        return WriteResult(False, "not an ops-writable user path", str(source))

    source_fd = destination_fd = None
    try:
        source_fd = _open_parent(user, source_relative, create=False)
        destination_fd = _open_parent(user, destination_relative)
        if _digest_at(source_fd, source_relative.name) != expected_digest:
            return WriteResult(False, "stale content digest", str(source))
        if _digest_at(destination_fd, destination_relative.name):
            return WriteResult(False, "destination exists", str(destination))
        if (
            _digest_at(source_fd, source_relative.name) != expected_digest
            or _digest_at(destination_fd, destination_relative.name)
        ):
            return WriteResult(False, "stale content digest", str(source))
        renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if renameat2 is None:
            return WriteResult(False, "atomic no-replace move unavailable", str(source))
        if renameat2(
            source_fd,
            os.fsencode(source_relative.name),
            destination_fd,
            os.fsencode(destination_relative.name),
            1,
        ):
            error = ctypes.get_errno()
            reason = "destination exists" if error == errno.EEXIST else os.strerror(error)
            return WriteResult(False, f"move failed: {reason}", str(source))
    except _UnsafeWritePath as exc:
        return WriteResult(False, str(exc), str(source))
    except OSError as exc:
        return WriteResult(False, f"move failed: {exc.strerror or type(exc).__name__}", str(source))
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    return WriteResult(True, "moved", str(destination))


def user_delete(
    target: str | Path,
    user_root: str | Path,
    expected_digest: str,
) -> WriteResult:
    user = Path(user_root).resolve()
    target = Path(target) if Path(target).is_absolute() else user / target
    relative = _ops_user_relative(user, target)
    if relative is None:
        return WriteResult(False, "not an ops-writable user path", str(target))

    parent_fd = None
    try:
        parent_fd = _open_parent(user, relative, create=False)
        if _digest_at(parent_fd, relative.name) != expected_digest:
            return WriteResult(False, "stale content digest", str(target))
        if _digest_at(parent_fd, relative.name) != expected_digest:
            return WriteResult(False, "stale content digest", str(target))
        os.unlink(relative.name, dir_fd=parent_fd)
    except _UnsafeWritePath as exc:
        return WriteResult(False, str(exc), str(target))
    except OSError as exc:
        return WriteResult(False, f"delete failed: {exc.strerror or type(exc).__name__}", str(target))
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
    return WriteResult(True, "deleted", str(target))


def collect_status(config: OpsConfig) -> dict:
    manifest = backup_manifest(config)
    return {
        "repos": manifest.repos,
        "proposals": [bucket.as_dict() for bucket in proposal_summary(config.agents)],
        "backup": backup_status(config, manifest).as_dict(),
        "user_revision": _git(config.user, "rev-parse", "HEAD") if (config.user / ".git").exists() else "",
    }


def dumps(data) -> str:
    return json.dumps(data, indent=2, sort_keys=True) + "\n"
