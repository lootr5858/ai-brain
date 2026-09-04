#!/usr/bin/env python3
"""brain-mcp dual-era STDIO transport tests."""
from __future__ import annotations

import json
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ENGINE = Path(__file__).resolve().parents[1]
STDIO = ENGINE / "servers" / "brain-mcp" / "stdio.py"
MODERN = "2026-07-28"
LEGACY = "2025-11-25"
root = Path(tempfile.mkdtemp())
brain = root / "brain"
for name in ("engine", "agents", "user"):
    (brain / name).mkdir(parents=True)
config = brain / "brain.config.toml"
config.write_text(
    "[paths]\n"
    f'brain = "{brain}"\n'
    f'engine = "{brain / "engine"}"\n'
    f'agents = "{brain / "agents"}"\n'
    f'user = "{brain / "user"}"\n'
)


def run_stdio(
    requests: list[dict[str, object]],
    *,
    expected_responses: int,
    timeout: int = 5,
) -> subprocess.CompletedProcess:
    payload = "".join(
        json.dumps(request, separators=(",", ":")) + "\n"
        for request in requests
    )
    process = subprocess.Popen(
        [sys.executable, str(STDIO), "--config", str(config)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    process.stdin.write(payload)
    process.stdin.flush()
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while len(lines) < expected_responses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            raise subprocess.TimeoutExpired(process.args, timeout)
        readable, _, _ = select.select(
            [process.stdout],
            [],
            [],
            remaining,
        )
        if not readable:
            process.kill()
            raise subprocess.TimeoutExpired(process.args, timeout)
        line = process.stdout.readline()
        if not line:
            break
        lines.append(line)
    process.stdin.close()
    try:
        returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    lines.extend(process.stdout.readlines())
    return subprocess.CompletedProcess(
        process.args,
        returncode,
        "".join(lines),
        process.stderr.read(),
    )


def responses_by_id(
    rows: list[dict[str, object]],
    expected_ids: set[object],
) -> dict[object, dict[str, object]]:
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), f"duplicate response IDs: {ids!r}"
    actual_ids = set(ids)
    assert actual_ids == expected_ids, (
        f"missing response IDs: {expected_ids - actual_ids!r}; "
        f"unexpected response IDs: {actual_ids - expected_ids!r}"
    )
    return {row["id"]: row for row in rows}


def modern_params(
    *,
    version: str = MODERN,
    values: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        **(values or {}),
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": version,
            "io.modelcontextprotocol/clientInfo": {
                "name": "brain-mcp-stdio-test",
                "version": "1",
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }


modern_requests = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": modern_params(),
    },
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": modern_params(),
    },
    {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": modern_params(
            values={"name": "health_check", "arguments": {}}
        ),
    },
    {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": modern_params(
            values={"name": "status", "arguments": {}}
        ),
    },
    {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": modern_params(
            values={"name": "list_context", "arguments": {}}
        ),
    },
    {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "ping",
        "params": modern_params(),
    },
]
r = run_stdio(modern_requests, expected_responses=6)
assert r.returncode == 0, r.stderr
rows = [json.loads(line) for line in r.stdout.splitlines()]
response_by_id = responses_by_id(rows, {1, 2, 3, 4, 5, 6})
discover = response_by_id[1]["result"]
assert MODERN in discover["supportedVersions"]
assert discover["capabilities"]["tools"] == {"listChanged": False}
assert discover["resultType"] == "complete"
listed = response_by_id[2]["result"]
assert listed["resultType"] == "complete"
assert listed["ttlMs"] >= 0
assert listed["cacheScope"] == "private"
names = {tool["name"] for tool in listed["tools"]}
assert "health_check" in names
assert "propose_change" not in names
called = response_by_id[3]["result"]
assert called["resultType"] == "complete"
assert called["content"][0]["text"] == "pong from brain-mcp"
status = json.loads(response_by_id[4]["result"]["content"][0]["text"])
assert status["transport"] == "stdio"
assert set(status["tools"]) == names
assert "# brain-mcp index" in response_by_id[5]["result"]["content"][0]["text"]
assert response_by_id[6]["error"]["code"] == -32601
print("task1 ok")

unsupported = run_stdio(
    [
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "server/discover",
            "params": modern_params(version="2099-01-01"),
        }
    ],
    expected_responses=1,
)
assert unsupported.returncode == 0, unsupported.stderr
unsupported_body = json.loads(unsupported.stdout)
assert unsupported_body["error"]["code"] == -32022
assert MODERN in json.dumps(unsupported_body["error"]["data"])
print("task2 ok")

legacy_requests = [
    {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "initialize",
        "params": {
            "protocolVersion": LEGACY,
            "capabilities": {},
            "clientInfo": {"name": "legacy-test", "version": "1"},
        },
    },
    {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    },
    {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/list",
        "params": {},
    },
    {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "tools/call",
        "params": {"name": "health_check", "arguments": {}},
    },
    {
        "jsonrpc": "2.0",
        "id": 13,
        "method": "tools/call",
        "params": {"name": "list_context", "arguments": {}},
    },
]
r = run_stdio(legacy_requests, expected_responses=4)
assert r.returncode == 0, r.stderr
rows = [json.loads(line) for line in r.stdout.splitlines()]
response_by_id = responses_by_id(rows, {10, 11, 12, 13})
assert response_by_id[10]["result"]["protocolVersion"] == LEGACY
assert response_by_id[10]["result"]["serverInfo"]["name"] == "brain-mcp"
assert "resultType" not in response_by_id[11]["result"]
assert "health_check" in {
    tool["name"] for tool in response_by_id[11]["result"]["tools"]
}
assert response_by_id[12]["result"]["content"][0]["text"] == "pong from brain-mcp"
assert "# brain-mcp index" in response_by_id[13]["result"]["content"][0]["text"]
print("task3 ok")

closed = subprocess.run(
    [sys.executable, str(STDIO), "--config", str(config)],
    input="",
    text=True,
    capture_output=True,
    timeout=3,
)
assert closed.returncode == 0, closed.stderr
assert closed.stdout == ""

bad_config = root / "invalid-brain.config.toml"
bad_config.write_text("[paths\n")
failed = subprocess.run(
    [sys.executable, str(STDIO), "--config", str(bad_config)],
    input="",
    text=True,
    capture_output=True,
    timeout=3,
)
assert failed.returncode != 0
assert failed.stdout == ""
assert "TOMLDecodeError" in failed.stderr
print("brain-mcp stdio tests: PASS")
