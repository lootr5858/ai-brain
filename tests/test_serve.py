"""`brain serve` for real: one process on a free loopback port, which paths are public, where it binds."""
from __future__ import annotations

import http.client
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from brain.__main__ import main

ENGINE = Path(__file__).resolve().parents[1]
HOST = "brain.test"
FLAGS = ["--public-url", f"https://{HOST}", "--cf-team", "team.cloudflareaccess.com", "--cf-aud", "aud"]
START_TIMEOUT = 15


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.brain, self.state = root / "brain", root / "state"
        (self.brain / "agents").mkdir(parents=True)
        (self.brain / "user").mkdir()
        (self.brain / "agents" / "README.md").write_text("# Agents\n")
        self.port = free_port()
        env = dict(os.environ, PYTHONPATH=str(ENGINE), XDG_STATE_HOME=str(self.state))
        self.server = subprocess.Popen(
            [sys.executable, "-P", "-m", "brain", "serve", "--brain", str(self.brain), *FLAGS, "--port", str(self.port)],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(self.stop)
        self.wait_until_listening()

    def stop(self) -> None:
        self.server.terminate()
        self.server.communicate(timeout=15)

    def wait_until_listening(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                self.fail(f"server exited early: {self.server.stdout.read()}")
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                time.sleep(0.05)
        self.fail("server did not start listening")

    def status(self, path: str, method: str = "GET") -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request(method, path, headers={"Host": HOST})
        return connection.getresponse().status

    def test_public_paths_and_owner_paths(self):
        for method, path, want in (
            ("GET", "/.well-known/oauth-authorization-server", 200),
            ("GET", "/.well-known/oauth-protected-resource/mcp", 200),
            ("POST", "/mcp", 401),
            ("GET", "/authorize", 403),
            ("GET", "/", 403),
            ("GET", "/api/tree", 403),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.status(path, method), want)

    def test_secret_is_owner_only(self):
        secret = self.state / "brain" / "secret"
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)


class BindTest(unittest.TestCase):
    def test_binds_loopback_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_STATE_HOME": tmp}), \
                mock.patch("uvicorn.run") as run:
            self.assertEqual(main(["serve", "--brain", tmp, *FLAGS, "--port", "9000"]), 0)
        self.assertEqual(run.call_args.kwargs, {"host": "127.0.0.1", "port": 9000})


if __name__ == "__main__":
    unittest.main()
