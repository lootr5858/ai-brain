"""Dashboard on the real app: owner gate, write proof, store rules through the API, MCP paths still public."""
from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from brain import app, auth
from brain.store import MAX_BYTES, Store
from brain.web.api import MAX_BODY
from test_auth import ISSUER, OTHER_KEY, OWNER, SECRET, access_check, access_jwt
from test_store import git, repo

SKILL = "---\nname: tone\ndescription: Write in the owner's voice.\n---\n\n# Tone\n"
PAGE = dict(OWNER, Origin=ISSUER)
READS = ["/", "/index.html", "/api/tree", "/api/file?path=profile.md", "/api/search?q=Sam",
         "/api/history?path=profile.md", "/api/diff?path=profile.md"]
WRITES = [("PUT", "/api/file"), ("POST", "/api/file"), ("POST", "/api/restore")]


class WebCase(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.brain = Path(tmp.name)
        agents, self.user = self.brain / "agents", self.brain / "user"
        for folder in ("core", "skills/tone/refs", "skills/.trash/old", "skills/synced/fetched",
                       "plugins/p/skills/linked"):
            (agents / folder).mkdir(parents=True)
        (self.user / "inventory").mkdir(parents=True)
        (agents / "README.md").write_text("# Agents\n")
        (agents / "core" / "style.md").write_text("Rule Sam.\n")
        (agents / "skills" / "tone" / "SKILL.md").write_text(SKILL)
        (agents / "skills" / "tone" / "refs" / "more.md").write_text("Extra Sam.\n")
        (agents / "skills" / ".trash" / "old" / "SKILL.md").write_text(SKILL)
        (agents / "skills" / "synced" / "fetched" / "SKILL.md").write_text(SKILL)
        (agents / "plugins" / "p" / "skills" / "linked" / "SKILL.md").write_text(SKILL)
        (agents / "skills" / "linked").symlink_to(agents / "plugins" / "p" / "skills" / "linked")
        (self.user / "profile.md").write_text("Name: Sam\n")
        (self.user / "inventory" / "items.yaml").write_text("- name: laptop\n")
        repo(self.user)
        oauth = auth.OAuth(ISSUER, SECRET, access_check(), fetch=lambda url: {})
        client = TestClient(app.create(self.brain, oauth), base_url=ISSUER, follow_redirects=False)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.client = client

    def file(self, path: str = "profile.md", **params) -> dict:
        response = self.client.get("/api/file", params={"path": path, **params}, headers=OWNER)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def save(self, text: str, base: str | None = None, path: str = "profile.md"):
        base = base if base is not None else self.file(path)["hash"]
        return self.client.put("/api/file", json={"path": path, "text": text, "base": base}, headers=PAGE)

    def subjects(self) -> list[str]:
        return git(self.user, "log", "--format=%s").splitlines()


class GateTest(WebCase):
    def test_no_login_refused_everywhere(self):
        for path in READS + ["/style.css", "/nothing/here", "/api/nothing"]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 403)
                self.assertTrue(response.json()["signin"])
        for method, path in WRITES:
            with self.subTest(path=path, method=method):
                self.assertEqual(self.client.request(method, path, json={}, headers={"Origin": ISSUER}).status_code, 403)

    def test_bad_login_refused(self):
        for token in (access_jwt(key=OTHER_KEY), access_jwt(aud="other"), access_jwt(iss="https://other"),
                      access_jwt(ttl=-10), "garbage"):
            with self.subTest(token=token[:12]):
                self.assertEqual(self.client.get("/api/tree", headers={"Cf-Access-Jwt-Assertion": token}).status_code, 403)

    def test_websocket_refused(self):
        with self.assertRaises(WebSocketDisconnect), self.client.websocket_connect("/api/tree", headers=OWNER):
            pass

    def test_write_needs_this_origin(self):
        for headers in (OWNER, dict(OWNER, Origin="https://evil.example"), dict(OWNER, Origin="null")):
            for method, path in WRITES:
                with self.subTest(origin=headers.get("Origin"), path=path, method=method):
                    response = self.client.request(method, path, json={"path": "profile.md"}, headers=headers)
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn("signin", response.json())
        self.assertEqual(self.subjects(), ["start"])

    def test_origin_compare_ignores_case(self):
        response = self.client.put("/api/file", json={"path": "profile.md", "text": "x", "base": self.file()["hash"]},
                                   headers=dict(OWNER, Origin=ISSUER.upper()))
        self.assertEqual(response.status_code, 200, response.text)

    def test_write_needs_json(self):
        body = '{"path": "new.md", "text": "x"}'
        for kind in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""):
            with self.subTest(kind=kind):
                response = self.client.post("/api/file", content=body, headers=dict(PAGE, **{"Content-Type": kind}))
                self.assertEqual(response.status_code, 415)
        self.assertFalse((self.user / "new.md").exists())

    def test_body_limit(self):
        response = self.client.post("/api/file", json={"path": "new.md", "text": "x" * MAX_BODY}, headers=PAGE)
        self.assertEqual(response.status_code, 413)
        self.assertFalse((self.user / "new.md").exists())

    def test_security_headers(self):
        for path in ("/", "/api/tree"):
            with self.subTest(path=path):
                headers = self.client.get(path, headers=OWNER).headers
                self.assertIn("default-src 'none'", headers["content-security-policy"])
                self.assertIn("script-src 'self'", headers["content-security-policy"])
                self.assertIn("img-src 'self'", headers["content-security-policy"])
                self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(self.client.get("/api/tree", headers=OWNER).headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/", headers=OWNER).headers["cache-control"], "no-cache")
        self.assertIn("default-src 'none'", self.client.get("/api/tree").headers["content-security-policy"])

    def test_public_paths_stay_public(self):
        self.assertEqual(self.client.get("/.well-known/oauth-authorization-server").status_code, 200)
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource/mcp").status_code, 200)
        response = self.client.post("/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.post("/token", data={"grant_type": "authorization_code"}).status_code, 401)


class ReadTest(WebCase):
    def test_page_served(self):
        response = self.client.get("/", headers=OWNER)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn('src="app.js"', response.text)

    def test_tree(self):
        (self.user / "profile.md").write_text("Name: Alex\n")
        tree = self.client.get("/api/tree", headers=OWNER).json()
        self.assertEqual(tree["user"], [
            {"path": "inventory/items.yaml", "size": 15, "dirty": False},
            {"path": "profile.md", "size": 11, "dirty": True},
        ])
        self.assertEqual([skill["name"] for skill in tree["skills"]], ["linked", "tone"])
        tone = tree["skills"][1]
        self.assertEqual(tone["description"], "Write in the owner's voice.")
        self.assertEqual(tone["files"], ["SKILL.md", "refs/more.md"])

    def test_tree_lists_an_unreadable_skill_without_a_description(self):
        (self.brain / "agents" / "skills" / "tone" / "SKILL.md").write_text("x" * (MAX_BYTES + 1))
        skills = {skill["name"]: skill for skill in self.client.get("/api/tree", headers=OWNER).json()["skills"]}
        self.assertEqual(skills["tone"]["description"], "")

    def test_read_user_file(self):
        data = self.file()
        self.assertEqual(data["text"], "Name: Sam\n")
        self.assertTrue(data["writable"])
        self.assertFalse(data["dirty"])
        (self.user / "profile.md").write_text("Name: Alex\n")
        self.assertTrue(self.file()["dirty"])

    def test_read_skill_file(self):
        for path in ("tone/SKILL.md", "tone/refs/more.md", "linked/SKILL.md"):
            with self.subTest(path=path):
                data = self.file(path, store="skills")
                self.assertFalse(data["writable"])

    def test_skill_read_limited_to_discovered_skills(self):
        for path in ("../core/style.md", "tone/../../core/style.md", "synced/fetched/SKILL.md", ".trash/old/SKILL.md",
                     "absent/SKILL.md", "../README.md"):
            with self.subTest(path=path):
                response = self.client.get("/api/file", params={"store": "skills", "path": path}, headers=OWNER)
                self.assertEqual(response.status_code, 400)

    def test_user_paths_refused(self):
        for path in ("../agents/README.md", ".git/config", "/etc/passwd", "absent.md", ""):
            with self.subTest(path=path):
                response = self.client.get("/api/file", params={"path": path}, headers=OWNER)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json())

    def test_search_covers_user_and_skills_only(self):
        hits = self.client.get("/api/search", params={"q": "sam"}, headers=OWNER).json()
        self.assertEqual(hits, [
            {"store": "user", "path": "profile.md", "line": 1, "text": "Name: Sam"},
            {"store": "skills", "path": "tone/refs/more.md", "line": 1, "text": "Extra Sam."},
        ])

    def test_search_too_short_refused(self):
        self.assertEqual(self.client.get("/api/search", params={"q": "a"}, headers=OWNER).status_code, 400)


class WriteTest(WebCase):
    def test_save_is_one_commit_via_dashboard(self):
        response = self.save("Name: Alex\n")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["commit"], git(self.user, "rev-parse", "--short", "HEAD"))
        self.assertEqual(response.json()["hash"], self.file()["hash"])
        self.assertEqual(self.subjects(), ["edit profile.md", "start"])
        self.assertEqual(git(self.user, "log", "-1", "--format=%b"), "via dashboard")

    def test_stale_base_is_409_and_keeps_the_newer_text(self):
        base = self.file()["hash"]
        Store(self.user).edit("profile.md", "Sam", "Alex", "via claude.ai")
        response = self.save("Name: Mine\n", base)
        self.assertEqual(response.status_code, 409)
        self.assertEqual((self.user / "profile.md").read_text(), "Name: Alex\n")
        self.assertEqual(self.save("Name: Mine\n").status_code, 200)

    def test_refusals_are_400(self):
        hash_ = self.file()["hash"]
        for path, text in (("profile.md", "Name: Sam\n"), ("profile.md", "x" * (MAX_BYTES + 1)),
                           ("../agents/README.md", "x"), (".git/config", "x")):
            with self.subTest(path=path, size=len(text)):
                self.assertEqual(self.save(text, hash_, path).status_code, 400)
        self.assertEqual(self.subjects(), ["start"])

    def test_bad_bodies_are_400(self):
        for body in ("not json", "[1]", '{"path": 1}', '{"path": "profile.md"}', '{"path": "\\ud800", "text": "x", "base": "y"}',
                     '{"path": "profile.md", "text": "\\ud800", "base": "y"}'):
            with self.subTest(body=body):
                response = self.client.put("/api/file", content=body, headers=dict(PAGE, **{"Content-Type": "application/json"}))
                self.assertEqual(response.status_code, 400, response.text)

    def test_skills_cannot_be_written(self):
        for method, path, body in (("PUT", "/api/file", {"path": "skills/tone/SKILL.md", "text": "x", "base": "y"}),
                                   ("POST", "/api/file", {"path": "skills/tone/new.md", "text": "x"})):
            with self.subTest(method=method):
                self.assertEqual(self.client.request(method, path, json=body, headers=PAGE).status_code, 400)
        self.assertEqual((self.brain / "agents" / "skills" / "tone" / "SKILL.md").read_text(), SKILL)
        self.assertFalse((self.brain / "agents" / "skills" / "tone" / "new.md").exists())

    def test_create(self):
        response = self.client.post("/api/file", json={"path": "inventory/books.yaml", "text": "- one\n"}, headers=PAGE)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.subjects(), ["create inventory/books.yaml", "start"])
        for path in ("profile.md", "new-folder/file.md"):
            with self.subTest(path=path):
                response = self.client.post("/api/file", json={"path": path, "text": "x"}, headers=PAGE)
                self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.subjects()), 2)

    def test_history_diff_restore(self):
        first = git(self.user, "rev-parse", "--short", "HEAD")
        self.save("Name: Alex\n")
        rows = self.client.get("/api/history", params={"path": "profile.md"}, headers=OWNER).json()
        self.assertEqual([(row["subject"], row["via"]) for row in rows], [("edit profile.md", "dashboard"), ("start", "")])
        diff = self.client.get("/api/diff", params={"path": "profile.md", "sha": rows[0]["sha"]}, headers=OWNER).json()["diff"]
        self.assertIn("+Name: Alex", diff)
        response = self.client.post("/api/restore", json={"path": "profile.md", "sha": first}, headers=PAGE)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.user / "profile.md").read_text(), "Name: Sam\n")
        self.assertEqual(response.json()["hash"], self.file()["hash"])
        self.assertEqual(git(self.user, "log", "-1", "--format=%s%n%b"), f"restore profile.md to {first}\nvia dashboard")

    def test_uncommitted_diff(self):
        (self.user / "profile.md").write_text("Name: Hand\n")
        diff = self.client.get("/api/diff", params={"path": "profile.md"}, headers=OWNER).json()["diff"]
        self.assertIn("+Name: Hand", diff)

    def test_commit_id_checked(self):
        for sha in ("--output=x", "HEAD"):
            with self.subTest(sha=sha):
                response = self.client.get("/api/diff", params={"path": "profile.md", "sha": sha}, headers=OWNER)
                self.assertEqual(response.status_code, 400)
                response = self.client.post("/api/restore", json={"path": "profile.md", "sha": sha}, headers=PAGE)
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
