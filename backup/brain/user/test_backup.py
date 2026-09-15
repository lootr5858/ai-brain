#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


def load_backup():
    path = Path(__file__).with_name("backup.py")
    spec = importlib.util.spec_from_file_location("brain_user_backup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRun:
    def __init__(
        self, *, cat_rc=0, fail_backup=False, fail_forget=False, fail_check=False,
        bad_restore=False,
    ):
        self.cat_rc = cat_rc
        self.fail_backup = fail_backup
        self.fail_forget = fail_forget
        self.fail_check = fail_check
        self.bad_restore = bad_restore
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if cmd[:3] == ["restic", "cat", "config"]:
            return Result(self.cat_rc)
        if cmd[:2] == ["restic", "backup"] and self.fail_backup:
            raise subprocess.CalledProcessError(42, cmd)
        if cmd[:2] == ["restic", "forget"] and self.fail_forget:
            raise subprocess.CalledProcessError(43, cmd)
        if cmd[:2] == ["restic", "check"] and self.fail_check:
            raise subprocess.CalledProcessError(44, cmd)
        if cmd[:2] == ["restic", "restore"]:
            target = Path(cmd[cmd.index("--target") + 1])
            brain = Path(kwargs["env"]["BRAIN_ROOT"])
            restored = target / str(brain).lstrip(os.sep)
            (restored / "user").mkdir(parents=True)
            (restored / "user" / "restored.md").write_text("restored\n")
            if not self.bad_restore:
                (restored / "brain.config.toml").write_text("# restored config\n")
        return Result()

    def commands(self):
        return [cmd for cmd, _ in self.calls]


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.brain = self.root / "brain"
        (self.brain / "user").mkdir(parents=True)
        (self.brain / "brain.config.toml").write_text("# config\n")
        self.password = self.root / "restic-password"
        self.password.write_text("not-a-real-password\n")
        self.env = {
            "HOME": str(self.root / "home"),
            "BRAIN_ROOT": str(self.brain),
            "RESTIC_REPOSITORY": "restic-secret-repository-value",
            "RESTIC_PASSWORD_FILE": str(self.password),
            "BRAIN_USER_BACKUP_ACK": "encrypted-user-data",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_backup(self, runner):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = load_backup().run_backup(env=self.env, run=runner)
        return rc, err.getvalue()

    def sentinel(self):
        return Path(self.env["HOME"]) / ".cache" / "brain-user-backup" / "backup.FAILED"

    def test_successful_backup_uses_exact_sources_retention_and_check(self):
        self.sentinel().parent.mkdir(parents=True)
        self.sentinel().write_text("old failure\n")
        runner = FakeRun()

        rc, _ = self.run_backup(runner)

        self.assertEqual(rc, 0)
        host = os.uname().nodename
        self.assertEqual(runner.commands()[0], ["restic", "cat", "config"])
        self.assertIn(
            [
                "restic",
                "backup",
                str(self.brain / "user"),
                str(self.brain / "brain.config.toml"),
                "--tag",
                "brain-user",
                "--host",
                host,
            ],
            runner.commands(),
        )
        self.assertTrue(any(cmd[:2] == ["restic", "forget"] for cmd in runner.commands()))
        self.assertEqual(runner.commands()[-1], ["restic", "check"])
        self.assertFalse(self.sentinel().exists())
        self.assertTrue((self.sentinel().parent / "backup.OK").exists())

    def test_missing_repository_is_initialized_before_backup(self):
        runner = FakeRun(cat_rc=10)

        rc, _ = self.run_backup(runner)

        self.assertEqual(rc, 0)
        self.assertEqual(runner.commands()[:3], [["restic", "cat", "config"], ["restic", "init"], runner.commands()[2]])
        self.assertEqual(runner.commands()[2][:2], ["restic", "backup"])

    def test_repository_access_error_aborts_before_init(self):
        for rc in (1, 11, 12, 99):
            with self.subTest(rc=rc):
                runner = FakeRun(cat_rc=rc)

                got, _ = self.run_backup(runner)

                self.assertEqual(got, 1)
                self.assertFalse(any(cmd[:2] == ["restic", "init"] for cmd in runner.commands()))
                self.assertFalse(any(cmd[:2] == ["restic", "backup"] for cmd in runner.commands()))

    def test_failed_backup_never_runs_retention_or_check(self):
        runner = FakeRun(fail_backup=True)

        rc, _ = self.run_backup(runner)

        self.assertEqual(rc, 1)
        self.assertFalse(any(cmd[:2] == ["restic", "forget"] for cmd in runner.commands()))
        self.assertFalse(any(cmd[:2] == ["restic", "check"] for cmd in runner.commands()))
        self.assertTrue(self.sentinel().exists())

    def test_failed_repository_check_does_not_record_success(self):
        rc, _ = self.run_backup(FakeRun(fail_check=True))

        self.assertEqual(rc, 1)
        self.assertTrue(self.sentinel().exists())
        self.assertFalse((self.sentinel().parent / "backup.OK").exists())

    def test_missing_ack_aborts_before_restic(self):
        self.env["BRAIN_USER_BACKUP_ACK"] = "nope"
        runner = FakeRun()

        rc, _ = self.run_backup(runner)

        self.assertEqual(rc, 1)
        self.assertFalse(any(cmd[:1] == ["restic"] for cmd in runner.commands()))

    def test_logs_do_not_expose_secret_environment_names_or_values(self):
        runner = FakeRun(fail_backup=True)

        rc, stderr = self.run_backup(runner)

        self.assertEqual(rc, 1)
        self.assertNotIn("RESTIC_REPOSITORY", stderr)
        self.assertNotIn("RESTIC_PASSWORD_FILE", stderr)
        self.assertNotIn(self.env["RESTIC_REPOSITORY"], stderr)
        self.assertNotIn(self.env["RESTIC_PASSWORD_FILE"], stderr)

    def test_staged_restore_is_verified_and_replacement_requires_confirmation(self):
        module = load_backup()
        (self.brain / "user" / "live.md").write_text("live\n")
        runner = FakeRun()

        self.assertEqual(module.stage_restore(env=self.env, run=runner), 0)
        stage = module.restore_stage(self.env)
        metadata = stage / "restore.json"
        self.assertTrue(metadata.exists())
        self.assertTrue((stage.parent / "restore.OK").exists())
        self.assertEqual(runner.commands()[-1][:3], ["restic", "restore", "latest"])

        self.assertEqual(module.replace_restore(env=self.env), 1)
        self.assertTrue((self.brain / "user" / "live.md").exists())
        self.assertEqual(module.replace_restore(env=self.env, confirmed=True), 0)
        self.assertFalse((self.brain / "user" / "live.md").exists())
        self.assertEqual((self.brain / "user" / "restored.md").read_text(), "restored\n")
        self.assertEqual((self.brain / "brain.config.toml").read_text(), "# restored config\n")
        self.assertFalse(stage.exists())

    def test_failed_restore_verification_keeps_current_data(self):
        module = load_backup()
        live = self.brain / "user" / "live.md"
        live.write_text("live\n")

        self.assertEqual(module.stage_restore(env=self.env, run=FakeRun(bad_restore=True)), 1)
        self.assertEqual(module.replace_restore(env=self.env, confirmed=True), 1)
        self.assertEqual(live.read_text(), "live\n")
        self.assertTrue(module.restore_stage(self.env).exists())

    def test_systemd_service_includes_local_bin_path(self):
        text = Path(__file__).with_name("brain-user-backup.service").read_text()

        self.assertIn("Environment=PATH=%h/.local/bin:", text)

    def test_systemd_service_uses_configured_engine_path(self):
        directory = Path(__file__).parent
        service = (directory / "brain-user-backup.service").read_text()
        environment = dict(
            line.split("=", 1)
            for line in (directory / "brain-user-backup.env.example").read_text().splitlines()
            if line
        )

        self.assertEqual(environment["BRAIN_ENGINE"], "/path/to/brain/engine")
        self.assertIn(
            "ExecStart=/usr/bin/python3 ${BRAIN_ENGINE}/backup/brain/user/backup.py",
            service,
        )
        self.assertNotIn("%h/brain", service)


if __name__ == "__main__":
    unittest.main(verbosity=2)
