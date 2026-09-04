#!/usr/bin/env python3
"""Durable credentials and configuration for remote brain-mcp access."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time


SCHEMA_VERSION = 1
AUTHORIZATION_CODE_LIFETIME = 5 * 60
ACCESS_TOKEN_LIFETIME = 60 * 60
REFRESH_TOKEN_LIFETIME = 30 * 24 * 60 * 60


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClientRecord:
    id: int
    client_id: str
    unique_name: str
    profile: str
    redirect_uri: str | None
    created_at: float


@dataclass(frozen=True)
class CreatedClient:
    client: ClientRecord
    client_secret: str


@dataclass(frozen=True)
class AuthorizationCodeRecord:
    client_id: str
    redirect_uri: str
    code_challenge: str
    resource: str
    scope: str
    expires_at: float


@dataclass(frozen=True)
class AccessTokenRecord:
    client_id: str
    resource: str
    scope: str
    expires_at: float


@dataclass(frozen=True)
class RefreshTokenRecord:
    client_id: str
    resource: str
    scope: str
    expires_at: float


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: float
    refresh_expires_at: float
    scope: str


def remote_state_path(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    values = os.environ if env is None else env
    base = values.get("XDG_STATE_HOME")
    state_home = Path(base) if base else (home or Path.home()) / ".local/state"
    return state_home / "brain-engine/brain-mcp/remote.sqlite3"


class RemoteStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or remote_state_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self._initialize()
        self.path.chmod(0o600)
        self.prune_expired()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise StoreError(f"unsupported remote state schema version {version}")
            if version == SCHEMA_VERSION:
                return
            tables = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if tables:
                raise StoreError("refusing unversioned remote state database")
            connection.executescript(
                """
                CREATE TABLE settings (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    canonical_resource TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE clients (
                    id INTEGER PRIMARY KEY,
                    client_id TEXT NOT NULL UNIQUE,
                    unique_name TEXT NOT NULL,
                    unique_name_folded TEXT NOT NULL UNIQUE,
                    profile TEXT NOT NULL,
                    secret_salt BLOB NOT NULL,
                    secret_hash BLOB NOT NULL,
                    redirect_uri TEXT,
                    created_at REAL NOT NULL
                );

                CREATE TABLE authorization_codes (
                    token_hash BLOB PRIMARY KEY,
                    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                    redirect_uri TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE access_tokens (
                    token_hash BLOB PRIMARY KEY,
                    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                    resource TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE refresh_tokens (
                    token_hash BLOB PRIMARY KEY,
                    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                    resource TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );

                PRAGMA user_version=1;
                """
            )

    @staticmethod
    def _client(row: sqlite3.Row) -> ClientRecord:
        return ClientRecord(
            id=row["id"],
            client_id=row["client_id"],
            unique_name=row["unique_name"],
            profile=row["profile"],
            redirect_uri=row["redirect_uri"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _secret_hash(secret: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            secret.encode(),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )

    def get_canonical_resource(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT canonical_resource FROM settings WHERE singleton_id=1"
            ).fetchone()
        return None if row is None else row["canonical_resource"]

    def set_canonical_resource(
        self,
        resource: str,
        *,
        now: float | None = None,
    ) -> str:
        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT canonical_resource FROM settings WHERE singleton_id=1"
            ).fetchone()
            if current is not None and current["canonical_resource"] == resource:
                return resource
            clients = connection.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
            if clients:
                raise StoreError(
                    "remove all remote clients before changing the canonical resource"
                )
            if current is None:
                connection.execute(
                    "INSERT INTO settings "
                    "(singleton_id, canonical_resource, created_at, updated_at) "
                    "VALUES (1, ?, ?, ?)",
                    (resource, timestamp, timestamp),
                )
            else:
                connection.execute(
                    "UPDATE settings SET canonical_resource=?, updated_at=? "
                    "WHERE singleton_id=1",
                    (resource, timestamp),
                )
        return resource

    def create_client(
        self,
        *,
        unique_name: str,
        profile: str,
        redirect_uri: str | None,
        now: float | None = None,
    ) -> CreatedClient:
        name = unique_name.strip()
        if not name:
            raise StoreError("client unique name must not be empty")
        timestamp = time.time() if now is None else now
        client_id = secrets.token_urlsafe(24)
        client_secret = secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        secret_hash = self._secret_hash(client_secret, salt)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO clients "
                    "(client_id, unique_name, unique_name_folded, profile, "
                    "secret_salt, secret_hash, redirect_uri, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        client_id,
                        name,
                        name.casefold(),
                        profile,
                        salt,
                        secret_hash,
                        redirect_uri,
                        timestamp,
                    ),
                )
                row_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise StoreError("client unique name already exists") from exc
        client = ClientRecord(
            id=row_id,
            client_id=client_id,
            unique_name=name,
            profile=profile,
            redirect_uri=redirect_uri,
            created_at=timestamp,
        )
        return CreatedClient(client=client, client_secret=client_secret)

    def get_client(self, client_id: str) -> ClientRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, client_id, unique_name, profile, redirect_uri, created_at "
                "FROM clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
        return None if row is None else self._client(row)

    def list_clients(self) -> list[ClientRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, client_id, unique_name, profile, redirect_uri, created_at "
                "FROM clients ORDER BY created_at, id"
            ).fetchall()
        return [self._client(row) for row in rows]

    def verify_client_secret(self, client_id: str, candidate: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT secret_salt, secret_hash FROM clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
        if row is None:
            return False
        candidate_hash = self._secret_hash(candidate, row["secret_salt"])
        return hmac.compare_digest(candidate_hash, row["secret_hash"])

    def remove_client(self, client_id: str) -> str | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT unique_name FROM clients WHERE client_id=?",
                (client_id,),
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM clients WHERE client_id=?", (client_id,))
        return row["unique_name"]

    def remove_all_clients(self) -> list[str]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT unique_name FROM clients ORDER BY created_at, id"
            ).fetchall()
            connection.execute("DELETE FROM clients")
        return [row["unique_name"] for row in rows]

    @staticmethod
    def _token_hash(token: str) -> bytes:
        return hashlib.sha256(token.encode()).digest()

    @staticmethod
    def _client_database_id(
        connection: sqlite3.Connection,
        client_id: str,
    ) -> int:
        row = connection.execute(
            "SELECT id FROM clients WHERE client_id=?",
            (client_id,),
        ).fetchone()
        if row is None:
            raise StoreError("unknown remote client")
        return row["id"]

    @staticmethod
    def _prune(connection: sqlite3.Connection, now: float) -> None:
        for table in ("authorization_codes", "access_tokens", "refresh_tokens"):
            connection.execute(f"DELETE FROM {table} WHERE expires_at<=?", (now,))

    def prune_expired(self, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            self._prune(connection, timestamp)

    def create_authorization_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        resource: str,
        scope: str,
        now: float | None = None,
    ) -> str:
        timestamp = time.time() if now is None else now
        code = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune(connection, timestamp)
            database_id = self._client_database_id(connection, client_id)
            connection.execute(
                "INSERT INTO authorization_codes "
                "(token_hash, client_id, redirect_uri, code_challenge, resource, "
                "scope, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._token_hash(code),
                    database_id,
                    redirect_uri,
                    code_challenge,
                    resource,
                    scope,
                    timestamp + AUTHORIZATION_CODE_LIFETIME,
                ),
            )
        return code

    def consume_authorization_code(
        self,
        code: str,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        resource: str,
        now: float | None = None,
    ) -> AuthorizationCodeRecord | None:
        timestamp = time.time() if now is None else now
        token_hash = self._token_hash(code)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT c.client_id, a.redirect_uri, a.code_challenge, a.resource, "
                "a.scope, a.expires_at "
                "FROM authorization_codes a "
                "JOIN clients c ON c.id=a.client_id WHERE a.token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None or row["client_id"] != client_id:
                return None
            connection.execute(
                "DELETE FROM authorization_codes WHERE token_hash=?",
                (token_hash,),
            )
            if (
                row["expires_at"] <= timestamp
                or row["redirect_uri"] != redirect_uri
                or row["code_challenge"] != code_challenge
                or row["resource"] != resource
            ):
                return None
        return AuthorizationCodeRecord(
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            code_challenge=row["code_challenge"],
            resource=row["resource"],
            scope=row["scope"],
            expires_at=row["expires_at"],
        )

    def load_authorization_code(
        self,
        code: str,
        *,
        client_id: str,
        now: float | None = None,
    ) -> AuthorizationCodeRecord | None:
        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.client_id, a.redirect_uri, a.code_challenge, a.resource, "
                "a.scope, a.expires_at FROM authorization_codes a "
                "JOIN clients c ON c.id=a.client_id "
                "WHERE a.token_hash=? AND c.client_id=? AND a.expires_at>?",
                (self._token_hash(code), client_id, timestamp),
            ).fetchone()
        if row is None:
            return None
        return AuthorizationCodeRecord(
            client_id=row["client_id"],
            redirect_uri=row["redirect_uri"],
            code_challenge=row["code_challenge"],
            resource=row["resource"],
            scope=row["scope"],
            expires_at=row["expires_at"],
        )

    @staticmethod
    def _insert_token_pair(
        connection: sqlite3.Connection,
        *,
        database_id: int,
        resource: str,
        scope: str,
        now: float,
    ) -> TokenPair:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        access_expires_at = now + ACCESS_TOKEN_LIFETIME
        refresh_expires_at = now + REFRESH_TOKEN_LIFETIME
        connection.execute(
            "INSERT INTO access_tokens "
            "(token_hash, client_id, resource, scope, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                RemoteStore._token_hash(access_token),
                database_id,
                resource,
                scope,
                access_expires_at,
            ),
        )
        connection.execute(
            "INSERT INTO refresh_tokens "
            "(token_hash, client_id, resource, scope, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                RemoteStore._token_hash(refresh_token),
                database_id,
                resource,
                scope,
                refresh_expires_at,
            ),
        )
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
            scope=scope,
        )

    def exchange_authorization_code(
        self,
        code: str,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        resource: str,
        now: float | None = None,
    ) -> TokenPair | None:
        timestamp = time.time() if now is None else now
        token_hash = self._token_hash(code)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT a.client_id, c.client_id AS public_client_id, "
                "a.redirect_uri, a.code_challenge, a.resource, a.scope, "
                "a.expires_at FROM authorization_codes a "
                "JOIN clients c ON c.id=a.client_id WHERE a.token_hash=?",
                (token_hash,),
            ).fetchone()
            if (
                row is None
                or row["public_client_id"] != client_id
                or row["redirect_uri"] != redirect_uri
                or row["code_challenge"] != code_challenge
                or row["resource"] != resource
                or row["expires_at"] <= timestamp
            ):
                return None
            tokens = self._insert_token_pair(
                connection,
                database_id=row["client_id"],
                resource=row["resource"],
                scope=row["scope"],
                now=timestamp,
            )
            connection.execute(
                "DELETE FROM authorization_codes WHERE token_hash=?",
                (token_hash,),
            )
            self._prune(connection, timestamp)
            return tokens

    def issue_tokens(
        self,
        *,
        client_id: str,
        resource: str,
        scope: str,
        now: float | None = None,
    ) -> TokenPair:
        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune(connection, timestamp)
            database_id = self._client_database_id(connection, client_id)
            return self._insert_token_pair(
                connection,
                database_id=database_id,
                resource=resource,
                scope=scope,
                now=timestamp,
            )

    def lookup_access_token(
        self,
        token: str,
        *,
        resource: str,
        now: float | None = None,
    ) -> AccessTokenRecord | None:
        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.client_id, a.resource, a.scope, a.expires_at "
                "FROM access_tokens a JOIN clients c ON c.id=a.client_id "
                "WHERE a.token_hash=? AND a.resource=? AND a.expires_at>?",
                (self._token_hash(token), resource, timestamp),
            ).fetchone()
        if row is None:
            return None
        return AccessTokenRecord(
            client_id=row["client_id"],
            resource=row["resource"],
            scope=row["scope"],
            expires_at=row["expires_at"],
        )

    def rotate_refresh_token(
        self,
        token: str,
        *,
        client_id: str,
        resource: str,
        scope: str | None = None,
        now: float | None = None,
    ) -> TokenPair | None:
        timestamp = time.time() if now is None else now
        token_hash = self._token_hash(token)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT r.client_id, c.client_id AS public_client_id, r.resource, "
                "r.scope, r.expires_at FROM refresh_tokens r "
                "JOIN clients c ON c.id=r.client_id WHERE r.token_hash=?",
                (token_hash,),
            ).fetchone()
            if (
                row is None
                or row["public_client_id"] != client_id
                or row["resource"] != resource
                or row["expires_at"] <= timestamp
            ):
                return None
            connection.execute(
                "DELETE FROM refresh_tokens WHERE token_hash=?",
                (token_hash,),
            )
            self._prune(connection, timestamp)
            return self._insert_token_pair(
                connection,
                database_id=row["client_id"],
                resource=row["resource"],
                scope=row["scope"] if scope is None else scope,
                now=timestamp,
            )

    def load_refresh_token(
        self,
        token: str,
        *,
        client_id: str,
        now: float | None = None,
    ) -> RefreshTokenRecord | None:
        timestamp = time.time() if now is None else now
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.client_id, r.resource, r.scope, r.expires_at "
                "FROM refresh_tokens r JOIN clients c ON c.id=r.client_id "
                "WHERE r.token_hash=? AND c.client_id=? AND r.expires_at>?",
                (self._token_hash(token), client_id, timestamp),
            ).fetchone()
        if row is None:
            return None
        return RefreshTokenRecord(
            client_id=row["client_id"],
            resource=row["resource"],
            scope=row["scope"],
            expires_at=row["expires_at"],
        )


__all__ = [
    "AccessTokenRecord",
    "AuthorizationCodeRecord",
    "ClientRecord",
    "CreatedClient",
    "RemoteStore",
    "RefreshTokenRecord",
    "StoreError",
    "TokenPair",
    "remote_state_path",
]
