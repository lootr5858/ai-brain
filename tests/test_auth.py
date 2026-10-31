"""OAuth flow on the real app: Cloudflare check, CIMD, consent, code, tokens, /mcp."""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from brain import app, auth
from test_store import git, repo

ISSUER = "http://testserver"
RESOURCE = f"{ISSUER}/mcp"
SECRET = "s" * 64
CLAUDE = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CHATGPT = "https://chatgpt.com/oauth/client.json"
CHATGPT_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
TEAM = "team.cloudflareaccess.com"
AUD = "aud-tag"
VERIFIER = "v" * 50
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def access_jwt(key=KEY, aud=AUD, iss=f"https://{TEAM}", email="owner@example.com", ttl=300) -> str:
    return jwt.encode({"aud": aud, "iss": iss, "email": email, "exp": int(time.time()) + ttl}, key, algorithm="RS256")


def access_check() -> auth.AccessCheck:
    check = auth.AccessCheck(TEAM, AUD)
    check.keys = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=KEY.public_key()))
    return check


OWNER = {"Cf-Access-Jwt-Assertion": access_jwt()}


class AuthCase(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.brain = Path(tmp.name)
        (self.brain / "agents").mkdir()
        (self.brain / "user").mkdir()
        (self.brain / "agents" / "README.md").write_text("# Agents\n")
        (self.brain / "user" / "profile.md").write_text("Name: Sam\n")
        repo(self.brain / "user")
        self.fetched: list[str] = []
        self.documents = {
            CLAUDE: {"client_id": CLAUDE, "redirect_uris": [CALLBACK]},
            CHATGPT: {"client_id": CHATGPT, "redirect_uris": [CHATGPT_CALLBACK]},
        }
        self.oauth = auth.OAuth(ISSUER, SECRET, access_check(), fetch=self.fetch)
        client = TestClient(app.create(self.brain, self.oauth), base_url=ISSUER, follow_redirects=False)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.client = client

    def fetch(self, url: str) -> dict:
        self.fetched.append(url)
        return self.documents[url] if url in self.documents else (_ for _ in ()).throw(OSError("no such document"))

    def params(self, **changes) -> dict:
        params = {"response_type": "code", "client_id": CLAUDE, "redirect_uri": CALLBACK, "state": "st8",
                  "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": RESOURCE,
                  "scope": "offline_access"}
        params.update(changes)
        return {key: value for key, value in params.items() if value is not None}

    def consent(self, **changes):
        return self.client.get("/authorize", params=self.params(**changes), headers=OWNER)

    def signed_request(self, **changes) -> str:
        response = self.consent(**changes)
        self.assertEqual(response.status_code, 200, response.text)
        return re.search(r'name="request" value="([^"]+)"', response.text).group(1)

    def decide(self, signed: str, decision: str = "allow"):
        return self.client.post("/authorize", data={"request": signed, "decision": decision}, headers=OWNER)

    def redirected(self, response) -> tuple[str, dict]:
        self.assertEqual(response.status_code, 302, response.text)
        parts = urlsplit(response.headers["location"])
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        return f"{parts.scheme}://{parts.netloc}{parts.path}", query

    def code(self, **changes) -> str:
        return self.redirected(self.decide(self.signed_request(**changes)))[1]["code"]

    def token(self, **data):
        return self.client.post("/token", data=data)

    def exchange(self, code: str, **changes):
        data = {"grant_type": "authorization_code", "code": code, "client_id": CLAUDE, "redirect_uri": CALLBACK,
                "code_verifier": VERIFIER, "resource": RESOURCE}
        data.update(changes)
        return self.token(**data)

    def tokens(self) -> dict:
        response = self.exchange(self.code())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def refresh(self, token: str):
        return self.token(grant_type="refresh_token", refresh_token=token, client_id=CLAUDE)

    def mcp(self, token: str | None, method: str = "tools/list", params: dict | None = None):
        headers = dict(MCP_HEADERS, **({"Authorization": f"Bearer {token}"} if token else {}))
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return self.client.post("/mcp", json=body, headers=headers)


class DiscoveryTest(AuthCase):
    def test_server_metadata(self):
        data = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(data["issuer"], ISSUER)
        self.assertEqual(data["token_endpoint_auth_methods_supported"], ["none"])
        self.assertEqual(data["code_challenge_methods_supported"], ["S256"])
        self.assertIs(data["client_id_metadata_document_supported"], True)
        self.assertIs(data["authorization_response_iss_parameter_supported"], True)
        self.assertNotIn("registration_endpoint", data)

    def test_resource_metadata_names_the_issuer_exactly(self):
        data = self.client.get("/.well-known/oauth-protected-resource/mcp").json()
        self.assertEqual(data["resource"], RESOURCE)
        self.assertEqual(data["authorization_servers"], [ISSUER])

    def test_mcp_without_token_points_at_metadata(self):
        response = self.mcp(None)
        self.assertEqual(response.status_code, 401)
        self.assertIn(f'resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"',
                      response.headers["www-authenticate"])

    def test_unused_endpoints_absent(self):
        self.assertEqual(self.client.post("/register", json={}).status_code, 403)
        self.assertEqual(self.client.post("/register", json={}, headers=dict(OWNER, Origin=ISSUER)).status_code, 405)


class OwnerTest(AuthCase):
    def test_missing_or_bad_access_jwt_refused(self):
        bad = {
            "none": {},
            "other key": {"Cf-Access-Jwt-Assertion": access_jwt(key=OTHER_KEY)},
            "other aud": {"Cf-Access-Jwt-Assertion": access_jwt(aud="other")},
            "other team": {"Cf-Access-Jwt-Assertion": access_jwt(iss="https://other.cloudflareaccess.com")},
            "expired": {"Cf-Access-Jwt-Assertion": access_jwt(ttl=-60)},
            "hs256": {"Cf-Access-Jwt-Assertion": jwt.encode({"aud": AUD, "iss": f"https://{TEAM}"}, "k" * 64, "HS256")},
            "garbage": {"Cf-Access-Jwt-Assertion": "not-a-jwt"},
        }
        for name, headers in bad.items():
            with self.subTest(name):
                self.assertEqual(self.client.get("/authorize", params=self.params(), headers=headers).status_code, 403)
                self.assertEqual(self.client.post("/authorize", data={"decision": "allow"}, headers=headers).status_code, 403)
        self.assertEqual(self.fetched, [])

    def test_key_server_failure_refused(self):
        def down(token):
            raise jwt.PyJWKClientConnectionError("down")
        self.oauth.owner.keys = SimpleNamespace(get_signing_key_from_jwt=down)
        self.assertEqual(self.consent().status_code, 403)


class ClientTest(AuthCase):
    def test_unknown_clients_never_fetched(self):
        for client_id in ("https://evil.example/client.json", "http://claude.ai/x", "https://claude.ai@evil.example/x",
                          "https://claude.ai:8443/x", "https://claude.ai/", "https://claude.ai/x#f", "", "https://[bad"):
            with self.subTest(client_id):
                self.assertEqual(self.consent(client_id=client_id).status_code, 400)
        self.assertEqual(self.fetched, [])

    def test_bad_documents_refused(self):
        wrong = "https://claude.ai/wrong"
        self.documents[wrong] = {"client_id": CLAUDE, "redirect_uris": [CALLBACK]}
        self.documents["https://claude.ai/other"] = {"client_id": "https://claude.ai/other", "redirect_uris": ["https://evil.example/cb"]}
        for client_id in (wrong, "https://claude.ai/other", "https://claude.ai/missing"):
            with self.subTest(client_id):
                self.assertEqual(self.consent(client_id=client_id).status_code, 400)

    def test_redirect_must_be_in_document_and_allowlist(self):
        self.documents[CLAUDE]["redirect_uris"].append("https://claude.ai/not-allowed")
        for uri in ("https://evil.example/cb", "https://claude.ai/not-allowed", CHATGPT_CALLBACK, ""):
            with self.subTest(uri):
                self.assertEqual(self.consent(redirect_uri=uri).status_code, 400)

    def test_document_cached(self):
        self.consent()
        self.consent()
        self.assertEqual(self.fetched, [CLAUDE])

    def test_both_apps_connect(self):
        self.signed_request()
        self.signed_request(client_id=CHATGPT, redirect_uri=CHATGPT_CALLBACK)


class ConsentTest(AuthCase):
    def test_request_errors_go_back_with_iss_and_state(self):
        cases = {
            "unsupported_response_type": {"response_type": "token"},
            "invalid_request": {"code_challenge": None},
            "invalid_target": {"resource": "https://other.example/mcp"},
        }
        for error, changes in cases.items():
            with self.subTest(error):
                target, query = self.redirected(self.consent(**changes))
                self.assertEqual(target, CALLBACK)
                self.assertEqual((query["error"], query["iss"], query["state"]), (error, ISSUER, "st8"))
        _, query = self.redirected(self.consent(code_challenge_method="plain"))
        self.assertEqual(query["error"], "invalid_request")

    def test_resource_optional(self):
        self.signed_request(resource=None)

    def test_page_names_hosts_from_urls_and_blocks_framing(self):
        response = self.consent()
        self.assertIn("<b>claude.ai</b>", response.text)
        self.assertIn("Signed in as owner@example.com", response.text)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

    def test_page_escapes_who_signed_in(self):
        headers = {"Cf-Access-Jwt-Assertion": access_jwt(email="<script>x</script>@example.com")}
        response = self.client.get("/authorize", params=self.params(), headers=headers)
        self.assertNotIn("<script>", response.text)

    def test_deny(self):
        target, query = self.redirected(self.decide(self.signed_request(), "deny"))
        self.assertEqual(target, CALLBACK)
        self.assertEqual((query["error"], query["iss"], query["state"]), ("access_denied", ISSUER, "st8"))
        self.assertNotIn("code", query)

    def test_forged_or_expired_request_refused(self):
        forged = jwt.encode({"typ": "consent", "exp": int(time.time()) + 60, "jti": "x", "aud": RESOURCE, "iss": ISSUER},
                            "o" * 64, "HS256")
        expired = self.oauth._sign("consent", -60, client_id=CLAUDE, redirect_uri=CALLBACK, state="s",
                                   code_challenge=CHALLENGE, scope="")
        code = self.code()
        for name, signed in (("forged", forged), ("expired", expired), ("a code", code), ("empty", "")):
            with self.subTest(name):
                self.assertEqual(self.decide(signed).status_code, 400)

    def test_allow_returns_code_with_iss_and_state(self):
        target, query = self.redirected(self.decide(self.signed_request()))
        self.assertEqual(target, CALLBACK)
        self.assertEqual((query["iss"], query["state"]), (ISSUER, "st8"))
        self.assertTrue(query["code"])


class TokenTest(AuthCase):
    def test_code_exchange(self):
        data = self.tokens()
        self.assertEqual(data["token_type"], "Bearer")
        self.assertEqual(data["expires_in"], auth.ACCESS_TTL)
        self.assertEqual(data["scope"], "offline_access")
        self.assertTrue(data["refresh_token"])

    def test_token_response_not_cached(self):
        self.assertEqual(self.exchange(self.code()).headers["cache-control"], "no-store")

    def test_code_works_once(self):
        code = self.code()
        self.assertEqual(self.exchange(code).status_code, 200)
        response = self.exchange(code)
        self.assertEqual((response.status_code, response.json()["error"]), (400, "invalid_grant"))

    def test_bad_exchanges_refused(self):
        cases = {
            "verifier": {"code_verifier": "w" * 50},
            "redirect": {"redirect_uri": CHATGPT_CALLBACK},
            "client": {"client_id": CHATGPT},
        }
        for name, changes in cases.items():
            with self.subTest(name):
                response = self.exchange(self.code(), **changes)
                self.assertEqual(response.status_code, 400, response.text)

    def test_unknown_client_refused(self):
        response = self.exchange(self.code(), client_id="https://evil.example/c.json")
        self.assertEqual((response.status_code, response.json()["error"]), (401, "invalid_client"))

    def test_wrong_token_kinds_refused(self):
        data = self.tokens()
        self.assertEqual(self.exchange(data["access_token"]).json()["error"], "invalid_grant")
        self.assertEqual(self.refresh(data["access_token"]).json()["error"], "invalid_grant")
        self.assertEqual(self.refresh(self.code()).json()["error"], "invalid_grant")

    def test_expired_code_refused(self):
        code = self.oauth._sign("code", -60, client_id=CLAUDE, sub="o", scope="", redirect_uri=CALLBACK,
                                code_challenge=CHALLENGE)
        self.assertEqual(self.exchange(code).json()["error"], "invalid_grant")

    def test_refresh_rotates_and_old_token_dies(self):
        first = self.tokens()
        response = self.refresh(first["refresh_token"])
        self.assertEqual(response.status_code, 200, response.text)
        second = response.json()
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        self.assertEqual(self.mcp(second["access_token"]).status_code, 200)
        stale = self.refresh(first["refresh_token"])
        self.assertEqual((stale.status_code, stale.json()["error"]), (400, "invalid_grant"))
        self.assertEqual(self.refresh(second["refresh_token"]).status_code, 200)

    def test_refresh_keeps_scope(self):
        first = self.tokens()
        second = self.token(grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=CLAUDE,
                            scope="offline_access")
        self.assertEqual(second.json()["scope"], "offline_access")

    def test_refresh_for_another_client_refused(self):
        response = self.token(grant_type="refresh_token", refresh_token=self.tokens()["refresh_token"], client_id=CHATGPT)
        self.assertEqual(response.json()["error"], "invalid_grant")


class BearerTest(AuthCase):
    def test_access_token_reaches_tools(self):
        response = self.mcp(self.tokens()["access_token"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("get_core", response.text)

    def test_other_tokens_refused(self):
        data = self.tokens()
        wrong_aud = jwt.encode({"iss": ISSUER, "aud": "https://other.example/mcp", "typ": "access", "jti": "x", "client_id": CLAUDE,
                                "sub": "o", "scope": "", "exp": int(time.time()) + 60}, SECRET, "HS256")
        wrong_key = jwt.encode({"iss": ISSUER, "aud": RESOURCE, "typ": "access", "jti": "x", "client_id": CLAUDE,
                                "sub": "o", "scope": "", "exp": int(time.time()) + 60}, "o" * 64, "HS256")
        tokens = {"refresh": data["refresh_token"], "wrong aud": wrong_aud, "wrong key": wrong_key,
                  "expired": self.oauth._sign("access", -60, client_id=CLAUDE, sub="o", scope=""), "garbage": "nope"}
        for name, token in tokens.items():
            with self.subTest(name):
                self.assertEqual(self.mcp(token).status_code, 401)

    def test_new_secret_revokes_everything(self):
        token = self.tokens()["access_token"]
        self.oauth.secret = "n" * 64
        self.assertEqual(self.mcp(token).status_code, 401)

    def test_other_host_refused(self):
        headers = dict(MCP_HEADERS, Authorization=f"Bearer {self.tokens()['access_token']}", Host="evil.example")
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        self.assertEqual(self.client.post("/mcp", json=body, headers=headers).status_code, 421)

    def test_origin_limited_to_the_apps(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        token = self.tokens()["access_token"]
        for origin, status in (("https://claude.ai", 200), ("https://chatgpt.com", 200), ("https://evil.example", 403)):
            with self.subTest(origin):
                headers = dict(MCP_HEADERS, Authorization=f"Bearer {token}", Origin=origin)
                self.assertEqual(self.client.post("/mcp", json=body, headers=headers).status_code, status)

    def test_write_names_the_client_in_the_commit(self):
        params = {"name": "edit", "arguments": {"path": "profile.md", "old": "Sam", "new": "Alex"}}
        response = self.mcp(self.tokens()["access_token"], "tools/call", params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("Saved profile.md", response.text)
        self.assertEqual(git(self.brain / "user", "log", "-1", "--format=%s%n%b").splitlines()[:2],
                         ["edit profile.md", "via claude.ai"])


class SecretTest(unittest.TestCase):
    def test_made_once_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state" / "brain"
            first = auth.load_secret(state)
            self.assertGreaterEqual(len(first), 60)
            self.assertEqual(auth.load_secret(state), first)
            self.assertEqual(stat.S_IMODE((state / "secret").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
