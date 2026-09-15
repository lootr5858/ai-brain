"""Plan and apply Brain skill/plugin changes. All writes require execute()."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import tempfile

from lib import fanout
from lib.plugins import Codex, copy_plugin, discover_plugins, fingerprint, identifier, read_object


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=".brain-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        Path(filename).replace(path)
    finally:
        Path(filename).unlink(missing_ok=True)


def unsafe_skills(skills):
    unsafe = {}
    for name, directory in skills.items():
        problems = list(fanout.validate_skill(directory))
        problems.extend(f"scan {f[0]}:{f[1]} [{f[2]}] {f[3]}"
                        for f in fanout.scan_skill(directory))
        if problems:
            unsafe[name] = problems
    return unsafe


class State:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError(f"state file must not be a symlink: {self.path}")
        self.data = read_object(self.path) if self.path.exists() else {
            "version": 1, "entries": {}, "markets": {}}
        if self.data.get("version") != 1 or any(
                not isinstance(self.data.get(k), dict) for k in ("entries", "markets")):
            raise ValueError(f"unsupported installation record: {self.path}")
        for entry_key, entry in self.entries.items():
            if not isinstance(entry, dict) or any(not isinstance(entry.get(k), str)
                    for k in ("name", "target", "source", "kind", "method", "digest")):
                raise ValueError(f"invalid installation record: {entry_key}")
            if entry["kind"] not in ("skills", "plugins") or entry["method"] not in ("symlink", "copy", "codex"):
                raise ValueError(f"invalid installation method: {entry_key}")
            if key(entry) != entry_key:
                raise ValueError(f"installation record key mismatch: {entry_key}")
            identifier(entry["name"])
            identifier(entry["target"], "target")
            paths = ("source", "market_root", "snapshot") if entry["method"] == "codex" else ("source", "destination")
            for name in paths:
                value = entry.get(name)
                if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
                    raise ValueError(f"invalid recorded {name}: {entry_key}")
            if entry["method"] == "codex":
                identifier(entry.get("marketplace"), "marketplace")
                if not isinstance(entry.get("executable"), str):
                    raise ValueError(f"invalid recorded executable: {entry_key}")
        for root, market in self.markets.items():
            if not isinstance(market, dict) or not Path(root).is_absolute():
                raise ValueError(f"invalid marketplace record: {root}")

    @property
    def entries(self):
        return self.data["entries"]

    @property
    def markets(self):
        return self.data["markets"]

    def save(self):
        write_json(self.path, self.data)


def key(entry):
    return f"{entry['target']}:{entry['kind']}:{entry['name']}"


def exists(path):
    return path.exists() or path.is_symlink()


def local_conflict(entry, previous=None):
    dest, source = Path(entry["destination"]), Path(entry["source"])
    # Never replace a source tree or write an installation inside it.
    parent = dest.parent.resolve()
    if parent == source or parent.is_relative_to(source) or source.is_relative_to(dest.absolute()):
        return f"destination overlaps source: {dest}"
    if not exists(dest):
        return None
    if dest.is_symlink():
        return None if dest.resolve() == source else f"unmanaged link: {dest}"
    marker = dest / ".brain-managed"
    if dest.is_dir() and marker.is_file() and not marker.is_symlink():
        if previous and fingerprint(dest) == previous.get("digest"):
            return None
        if previous is None and marker.read_text() == "":
            return None  # legacy installer copy
        return f"managed copy changed or has no installation record: {dest}"
    return f"unmanaged destination: {dest}"


@dataclass
class Plan:
    state: State
    actions: list[dict] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)
    settings: Path | None = None
    uninstall: bool = False

    @property
    def blocked(self):
        return any(line.startswith("REFUSE") for line in self.reports)


def make_plan(cfg, *, targets=None, kind="all", uninstall=False, root=None, settings=None):
    if kind not in ("skills", "plugins", "all"):
        raise ValueError(f"unknown kind: {kind}")
    fan = cfg.get("engine", {}).get("fanout", {})
    mode = fan.get("mode", "symlink")
    if mode not in ("symlink", "copy"):
        raise ValueError("engine.fanout.mode must be symlink or copy")
    if root is not None and kind != "skills":
        raise ValueError("--root requires --kind skills; it cannot redirect native plugin installations")
    root = Path(root).expanduser().absolute() if root else None
    state_path = Path(root) / "state.json" if root else Path(
        fan.get("state", str(Path(cfg["paths"]["brain"]) / ".brain-install/state.json")))
    state = State(state_path)
    plan = Plan(state, uninstall=uninstall)
    destinations = {name: Path(root) / name if root else Path(path)
                    for name, path in fan.get("targets", {}).items()}
    known = set(destinations)
    if uninstall:
        known.update(e["target"] for e in state.entries.values())
    selected = set(targets) if targets else known
    if selected - known:
        raise ValueError(f"unknown target(s): {', '.join(sorted(selected - known))}")
    for name in selected:
        identifier(name, "target")
    if uninstall:
        for entry in state.entries.values():
            if entry["target"] in selected and (kind == "all" or entry["kind"] == kind):
                plan.actions.append(dict(entry))
        # Preserve removal support for links/copies created by the old installer.
        if kind != "plugins":
            for name, source in fanout.load_skills(cfg).items():
                for target in sorted(selected & set(destinations)):
                    entry = dict(name=name, source=str(source.resolve()), target=target,
                                 kind="skills", method=mode,
                                 destination=str(destinations[target] / name))
                    if key(entry) not in state.entries and exists(Path(entry["destination"])):
                        plan.actions.append(entry)
    else:
        if kind != "plugins":
            skills = fanout.load_skills(cfg)
            unsafe = unsafe_skills(skills)
            for name, issues in unsafe.items():
                plan.reports.extend(f"REFUSE skill {name}: {issue}" for issue in issues)
            for target in sorted(selected):
                for name, source in skills.items():
                    if name not in unsafe:
                        plan.actions.append(dict(name=name, source=str(source.resolve()), target=target,
                                                 kind="skills", method=mode, digest=fingerprint(source),
                                                 destination=str(destinations[target] / name)))
            if "claude-code" in selected and skills:
                plan.settings = Path(settings) if settings else (
                    Path(root) / "settings.json" if root else Path.home() / ".claude/settings.json")
                if plan.settings.exists():
                    read_object(plan.settings)  # fail before placing anything
        if kind != "skills":
            plugin_cfg = fan.get("plugins", {})
            for target in sorted(selected):
                options = plugin_cfg.get(target)
                if not options:
                    plan.reports.append(f"SKIP {target} plugins: no runtime configured")
                    continue
                runtime = options["runtime"]
                plugins, reports = discover_plugins(Path(cfg["paths"]["agents"]), runtime,
                                                     exclude=options.get("exclude", []))
                plan.reports.extend(reports)
                for plugin in plugins.values():
                    entry = dict(name=plugin.name, source=str(plugin.root), target=target,
                                 kind="plugins", method=mode if runtime == "claude-code" else "codex",
                                 digest=plugin.digest, skills=list(plugin.skills), hooks=list(plugin.hooks))
                    if runtime == "claude-code":
                        entry["destination"] = str(destinations[target] / plugin.name)
                    else:
                        entry.update(marketplace=options["marketplace_name"],
                                     market_root=options["marketplace"],
                                     executable=options.get("executable", "codex"))
                    plan.actions.append(entry)

    occupied = {}
    for entry in plan.actions:
        previous = state.entries.get(key(entry))
        if previous and any(previous.get(k) != entry.get(k) for k in
                            ("source", "destination", "market_root", "marketplace", "executable")):
            plan.reports.append(f"REFUSE {entry['name']}: installation location changed; uninstall old placement first")
        if entry["method"] != "codex":
            dest = str(Path(entry["destination"]).parent.resolve() / entry["name"])
            if dest in occupied and occupied[dest] != key(entry):
                plan.reports.append(f"REFUSE destination collision: {dest}")
            occupied[dest] = key(entry)
            conflict = local_conflict(entry, previous)
            if conflict:
                plan.reports.append(f"REFUSE {entry['target']} {entry['name']}: {conflict}")
            verb = "REMOVE" if uninstall else ("LINK" if entry["method"] == "symlink" else "COPY")
            plan.reports.append(f"{verb} {entry['target']} {entry['kind']} {entry['name']}: {dest} -> {entry['source']}")
        else:
            verb = "UNINSTALL" if uninstall else "INSTALL/REFRESH"
            plan.reports.append(f"{verb} {entry['target']} plugin {entry['name']}@{entry['marketplace']}: "
                                f"snapshot from {entry['source']} in {entry['market_root']}")
            plan.reports.append("INFO native ownership and installed-state checks occur on --apply; dry-run runs no CLI")
            check_market_files(state, entry)
        if entry.get("skills"):
            plan.reports.append(f"INFO {entry['name']} bundled skills: {', '.join(entry['skills'])}")
        if entry.get("hooks"):
            plan.reports.append(f"INFO {entry['name']} hooks: {', '.join(entry['hooks'])}; activation/trust remains with the application")
    if plan.settings:
        plan.reports.append(f"SET skillListingBudgetFraction=0.025 in {plan.settings}")
    if not plan.actions:
        plan.reports.append("INFO nothing to install/remove")
    return plan


def market_marker(state, entry):
    return {"owner": str(state.path.absolute()), "name": entry["marketplace"]}


def check_market_files(state, entry):
    root = Path(entry["market_root"])
    source = Path(entry["source"])
    if root == source or root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError(f"marketplace overlaps source plugin: {root}")
    marker = root / ".brain-managed"
    if root.is_symlink():
        raise ValueError(f"marketplace root must not be a symlink: {root}")
    for relative in ("plugins", ".agents", ".agents/plugins", ".agents/plugins/marketplace.json"):
        if (root / relative).is_symlink():
            raise ValueError(f"managed marketplace path must not be a symlink: {root / relative}")
    if root.exists() and any(root.iterdir()):
        if marker.is_symlink() or not marker.is_file() or read_object(marker) != market_marker(state, entry):
            raise ValueError(f"unmanaged marketplace directory: {root}")
    catalog = root / ".agents/plugins/marketplace.json"
    previous = state.markets.get(str(root), {})
    if catalog.exists() and (catalog.is_symlink() or read_object(catalog) != previous.get("catalog")):
        raise ValueError(f"marketplace catalog changed outside installer: {catalog}")
    for record in state.entries.values():
        if record.get("market_root") == str(root) and record.get("snapshot"):
            snapshot = Path(record["snapshot"])
            if snapshot.parent != root / "plugins" or snapshot.is_symlink():
                raise ValueError(f"invalid snapshot path: {snapshot}")
            if snapshot.exists() and fingerprint(snapshot) != record["digest"]:
                raise ValueError(f"managed plugin snapshot was edited: {snapshot}")


def replace_local(entry, state):
    dest, source = Path(entry["destination"]), Path(entry["source"])
    if entry["method"] == "symlink" and dest.is_symlink() and dest.resolve() == source:
        return
    if entry["method"] == "copy" and dest.is_dir() and not dest.is_symlink():
        if fingerprint(dest) == entry["digest"]:
            return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".brain-place-", dir=dest.parent) as tmp:
        prepared = Path(tmp) / "new"
        if entry["method"] == "symlink":
            prepared.symlink_to(source)
        else:
            copy_plugin(source, prepared)
            write_json(prepared / ".brain-managed", {"owner": str(state.path), "source": str(source)})
        old = Path(tmp) / "old"
        if exists(dest):
            dest.rename(old)
        try:
            prepared.rename(dest)
        except OSError:
            if exists(old):
                old.rename(dest)
            raise


def remove_local(entry):
    dest = Path(entry["destination"])
    if dest.is_symlink():
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)


def preflight_native(plan, factory):
    inventories = {}
    for entry in plan.actions:
        if entry["method"] != "codex":
            continue
        exe = entry["executable"]
        if exe not in inventories:
            client = factory(exe)
            inventories[exe] = (client, *client.inventory())
        _, markets, installed = inventories[exe]
        root, name = entry["market_root"], entry["marketplace"]
        previous_market = plan.state.markets.get(root)
        for market in markets:
            if market["name"] == name and (Path(market["root"]).resolve() != Path(root).resolve()
                                           or not previous_market or not previous_market.get("registration")):
                raise ValueError(f"unmanaged Codex marketplace: {name}")
        for plugin in installed:
            if plugin["name"] == entry["name"] and plugin["marketplaceName"] == name:
                if key(entry) not in plan.state.entries:
                    raise ValueError(f"unmanaged Codex plugin: {entry['name']}@{name}")
    return inventories


def catalog_for(state, entry):
    records = [e for e in state.entries.values() if e.get("market_root") == entry["market_root"]]
    return {"name": entry["marketplace"], "interface": {"displayName": "Brain local plugins"},
            "plugins": [{"name": e["name"], "source": {"source": "local", "path":
                        "./" + str(Path(e["snapshot"]).relative_to(entry["market_root"]))},
                         "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                         "category": "Productivity"} for e in sorted(records, key=lambda e: e["name"])]}


def save_catalog(state, entry):
    root = Path(entry["market_root"])
    market = state.markets.setdefault(str(root), {"name": entry["marketplace"]})
    catalog = catalog_for(state, entry)
    write_json(root / ".agents/plugins/marketplace.json", catalog)
    market["catalog"] = catalog
    state.save()


def apply_native(plan, entry, inventory):
    state = plan.state
    client, markets, installed = inventory
    root = Path(entry["market_root"])
    selector = f"{entry['name']}@{entry['marketplace']}"
    previous = state.entries.get(key(entry))
    current = next((p for p in installed if p["name"] == entry["name"]
                    and p["marketplaceName"] == entry["marketplace"]), None)
    if plan.uninstall:
        if current:
            client.call("remove", selector)
            installed.remove(current)
        snapshot = Path(entry["snapshot"]) if entry.get("snapshot") else None
        if snapshot and snapshot.exists():
            shutil.rmtree(snapshot)
        state.entries.pop(key(entry), None)
        save_catalog(state, entry)
        return
    if current and previous and previous.get("digest") == entry["digest"] and previous.get("status") == "installed":
        plan.reports.append(f"UNCHANGED {selector}" + (" (disabled in Codex; preserved)" if current.get("enabled") is False else ""))
        return
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / ".brain-managed", market_marker(state, entry))
    snapshot = root / "plugins" / f"{entry['name']}-{entry['digest']}"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    if exists(snapshot):
        if snapshot.is_symlink() or fingerprint(snapshot) != entry["digest"]:
            raise ValueError(f"unmanaged or changed plugin snapshot: {snapshot}")
    else:
        with tempfile.TemporaryDirectory(prefix=".brain-snapshot-", dir=snapshot.parent) as tmp:
            prepared = Path(tmp) / "new"
            copy_plugin(Path(entry["source"]), prepared)
            if fingerprint(prepared) != entry["digest"]:
                raise ValueError(f"source changed while copying: {entry['source']}")
            prepared.rename(snapshot)
    record = dict(entry, snapshot=str(snapshot), status="pending")
    state.entries[key(entry)] = record
    state.save()  # journal ownership before native mutation, including uncertain failures
    save_catalog(state, entry)
    market = state.markets[str(root)]
    if not any(m["name"] == entry["marketplace"] for m in markets):
        market["registration"] = "pending"
        state.save()
        result = client.call("marketplace", "add", str(root))
        if result.get("marketplaceName") != entry["marketplace"]:
            raise RuntimeError("Codex registered an unexpected marketplace; inspect native state")
        market["registration"] = "installed"
        markets.append({"name": entry["marketplace"], "root": str(root)})
        state.save()
    if current:
        client.call("remove", selector)  # native removal invalidates the previous cached version
        installed.remove(current)
    result = client.call("add", selector)
    if result.get("name") != entry["name"] or result.get("marketplaceName") != entry["marketplace"]:
        raise RuntimeError(f"Codex returned an unexpected install result for {selector}")
    record["status"] = "installed"
    state.save()
    installed.append({"name": entry["name"], "marketplaceName": entry["marketplace"], "enabled": True})
    if previous and previous.get("snapshot") != str(snapshot) and previous.get("snapshot"):
        old = Path(previous["snapshot"])
        if old.exists():
            shutil.rmtree(old)
    plan.reports.append(f"INSTALLED {selector}; start a new task and review/trust bundled hooks")


def cleanup_markets(plan, inventories):
    for entry in plan.actions:
        if entry["method"] != "codex":
            continue
        root = entry["market_root"]
        if root not in plan.state.markets or any(e.get("market_root") == root for e in plan.state.entries.values()):
            continue
        client, markets, installed = inventories[entry["executable"]]
        if any(p["marketplaceName"] == entry["marketplace"] for p in installed):
            plan.reports.append(f"KEEP marketplace {entry['marketplace']}: other installed plugins remain")
            continue
        if any(m["name"] == entry["marketplace"] for m in markets):
            client.call("marketplace", "remove", entry["marketplace"])
            markets[:] = [m for m in markets if m["name"] != entry["marketplace"]]
        for relative in (".agents/plugins/marketplace.json", ".brain-managed"):
            (Path(root) / relative).unlink(missing_ok=True)
        for relative in (".agents/plugins", ".agents", "plugins", "."):
            directory = Path(root) / relative
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        del plan.state.markets[root]
        plan.state.save()


def execute(plan, *, codex_factory=Codex):
    """Explicit mutation boundary. CLI calls this only when --apply wins."""
    if plan.blocked:
        raise ValueError("plan contains refusals; no changes applied")
    # Recheck immediately before writes; another process may have changed a destination.
    for entry in plan.actions:
        if entry["method"] == "codex":
            check_market_files(plan.state, entry)
        else:
            conflict = local_conflict(entry, plan.state.entries.get(key(entry)))
            if conflict:
                raise ValueError(conflict)
        if not plan.uninstall and fingerprint(Path(entry["source"])) != entry["digest"]:
            raise ValueError(f"source changed since planning: {entry['source']}")
    inventories = preflight_native(plan, codex_factory)
    for entry in plan.actions:
        if entry["method"] == "codex":
            apply_native(plan, entry, inventories[entry["executable"]])
        elif plan.uninstall:
            remove_local(entry)
            plan.state.entries.pop(key(entry), None)
            plan.state.save()
        else:
            replace_local(entry, plan.state)
            plan.state.entries[key(entry)] = dict(entry)
            plan.state.save()
    if plan.uninstall:
        cleanup_markets(plan, inventories)
    if plan.settings:
        settings = read_object(plan.settings) if plan.settings.exists() else {}
        if settings.get("skillListingBudgetFraction") != 0.025:
            settings["skillListingBudgetFraction"] = 0.025
            write_json(plan.settings, settings)
