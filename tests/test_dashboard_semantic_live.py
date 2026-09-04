#!/usr/bin/env python3
"""Opt-in local-provider check; synthetic files and a disposable catalog only."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard.content import ContentCatalog
from dashboard.server import open_repository_file
from lib.ops import load_ops_config


def unavailable(_texts):
    raise OSError("simulated provider outage")


with tempfile.TemporaryDirectory(prefix="brain-semantic-live-") as temporary:
    root = Path(temporary)
    user, agents = root / "user", root / "agents"
    for repository in (user, agents):
        repository.mkdir()
        subprocess.run(["git", "-C", str(repository), "init", "-q"], check=True)
    (user / "knowledge").mkdir()
    fixtures = {
        "cycling.md": "# Storage\nBicycles must be secured inside the locked shed beside the garage.\n",
        "baking.md": "# Kitchen\nBread dough is made from flour, water, yeast and salt, then baked in an oven.\n",
        "manual.md": "# Operations manual\n" + "General office procedures and stationery ordering.\n\n" * 300
        + "If your laptop is stolen, immediately revoke its access credentials and notify the administrator.\n",
        "unicode.md": "# Multilingual notes\n" + "自転車の保管場所 🚲 café.\n\n" * 300,
    }
    for name, body in fixtures.items():
        (user / "knowledge" / name).write_text(body)
    config = replace(load_ops_config(), user=user, agents=agents)
    catalog = ContentCatalog(config, open_repository_file, root / "catalog.sqlite3", embed=unavailable)
    assert catalog._semantic_configured(), "Configure the local embedding URL and model first."
    assert catalog.rebuild()["semantic_available"] is False
    assert catalog.search("Bicycles?", "semantic")["results"][0]["path"] == "knowledge/cycling.md"

    catalog.embed = None
    started = time.monotonic()
    cases = (
        ("Where can I leave my bike securely?", "cycling.md", "locked shed"),
        ("How do I prepare bread?", "baking.md", "flour"),
        ("How do I respond to a stolen computer?", "manual.md", "revoke its access credentials"),
    )
    for query, name, passage in cases:
        result = catalog.search(query, "semantic")
        assert result["semantic_available"], "Semantic search fell back instead of completing."
        assert result["results"][0]["path"] == "knowledge/" + name
        assert passage in result["results"][0]["snippet"]
        assert len({row["path"] for row in result["results"]}) == len(fixtures)
    with sqlite3.connect(catalog.database_path) as connection:
        chunks = connection.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    print(f"live semantic retrieval: PASS ({len(fixtures)} pages, {chunks} cached chunks, "
          f"{time.monotonic() - started:.2f}s)")

    restarted = ContentCatalog(config, open_repository_file, catalog.database_path)
    with patch.object(restarted, "_embed", side_effect=AssertionError("cache was not reused")):
        assert restarted.rebuild()["semantic_available"] is True
    restarted.embed = unavailable
    assert restarted.search("Bicycles?", "semantic")["semantic_available"] is False
    restarted.embed = None
    assert restarted.search(cases[0][0], "semantic")["semantic_available"] is True
    assert all((user / "knowledge" / name).read_text() == body for name, body in fixtures.items())
    print("live cache reuse and provider recovery: PASS (source files unchanged)")
