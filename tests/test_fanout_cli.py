"""Optional CLI integration checks. Explicit opt-in; not part of the smoke suite."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    if sys.argv[1:] != ["--run-cli-tests"]:
        print("SKIP: pass --run-cli-tests to launch skills.py against temporary skill fixtures")
        return
    engine = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="brain-cli-test-") as tmp:
        root = Path(tmp).resolve()
        source = root / "agents/skills/demo"
        source.mkdir(parents=True)
        (source / "SKILL.md").write_text("---\nname: demo\ndescription: Fixture.\n---\n")
        config = root / "config.toml"
        config.write_text(f'''[paths]
brain = "{root}"
agents = "{root / 'agents'}"
[engine.fanout.targets]
claude-code = "{root / 'unused'}"
codex-dir = "{root / 'unused2'}"
''')
        base = [sys.executable, str(engine / "install/skills.py"), "--config", str(config),
                "--kind", "skills", "--root", str(root / "targets")]
        def run(*args):
            result = subprocess.run([*base, *args], capture_output=True, text=True, check=False)
            assert result.returncode == 0, result.stdout + result.stderr
        run("--apply", "--dry-run")
        assert not (root / "targets").exists()
        run("--apply")
        run("--apply")
        assert (root / "targets/codex-dir/demo").resolve() == source
        settings = json.loads((root / "targets/settings.json").read_text())
        assert settings["skillListingBudgetFraction"] == 0.025
        run("--uninstall", "--apply")
        assert not (root / "targets/codex-dir/demo").is_symlink()
    print("CLI integration: PASS")


if __name__ == "__main__":
    main()
