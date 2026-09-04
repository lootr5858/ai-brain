#!/usr/bin/env python3
"""Dashboard brain-mcp adapter tests - run with the engine virtual environment."""
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.brain_mcp_remote_store import RemoteStore, StoreError
from lib.ops import brain_mcp_action, brain_mcp_status


store = RemoteStore(Path(tempfile.mkdtemp()) / "remote.sqlite3")
uninitialized = brain_mcp_status(store=store)
assert uninitialized["error"] == ""
assert uninitialized["remote"] == {"configured": False, "resource": "", "clients": []}
with patch("lib.brain_mcp_remote_store.remote_state_path", return_value=store.path.parent / "absent.sqlite3"):
    assert brain_mcp_status()["remote"] == uninitialized["remote"]
store.set_canonical_resource("https://brain.example/mcp", now=1)
created = brain_mcp_action(
    "add-client",
    name="Dashboard Claude",
    profile="claude",
    store=store,
)
assert created["client_secret"]
with patch("lib.ops.service_status", side_effect=AssertionError("client status must not inspect services")):
    status = brain_mcp_status(store=store)
assert set(status) == {"subsystem", "observed_time", "error", "remote"}
assert status["error"] == ""
assert set(status["remote"]) == {"configured", "resource", "clients"}
assert status["remote"]["resource"] == "https://brain.example/mcp"
assert len(status["remote"]["clients"]) == 1
assert status["remote"]["clients"][0]["name"] == "Dashboard Claude"
assert status["remote"]["clients"][0]["profile"] == "claude"
assert created["client_id"] not in str(status)
assert created["client_secret"] not in str(status)

for method in ("get_canonical_resource", "list_clients"):
    for error in (OSError("private path"), sqlite3.DatabaseError("private path"), StoreError("private path")):
        with patch.object(store, method, side_effect=error):
            unavailable = brain_mcp_status(store=store)
        assert unavailable["error"] == "remote state is unavailable"
        assert unavailable["remote"] == uninitialized["remote"]
        assert "private path" not in str(unavailable)

for action in ("verify-local", "start", "stop", "restart"):
    try:
        brain_mcp_action(action, store=store)
    except ValueError as exc:
        assert str(exc) == "unsupported brain-mcp action"
    else:
        raise AssertionError("dashboard MCP actions must only manage remote clients")

try:
    brain_mcp_action("revoke-client", name="Dashboard Claude", store=store)
except ValueError:
    pass
else:
    raise AssertionError("client revocation must require confirmation")

revoked = brain_mcp_action(
    "revoke-client",
    name="dashboard claude",
    confirmed=True,
    store=store,
)
assert revoked == {"action": "revoke-client", "name": "Dashboard Claude"}
assert store.list_clients() == []

print("dashboard brain-mcp tests: PASS")
