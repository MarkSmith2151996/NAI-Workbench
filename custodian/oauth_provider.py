#!/usr/bin/env python3
"""Persistent OAuth provider for Custodian MCP HTTP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from typing import Any
from uuid import uuid4

import bcrypt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from custodian.db.connection import db_connection

ACCESS_TOKEN_PREFIX = "custodian_at_"
REFRESH_TOKEN_PREFIX = "custodian_rt_"
AUTH_CODE_PREFIX = "custodian_code_"
DEFAULT_SCOPES = ["custodian.full"]
ACCESS_TOKEN_TTL = 3600
AUTH_CODE_TTL = 600


class CustodianAccessToken(AccessToken):
    token_id: str
    family_id: str | None = None


class CustodianRefreshToken(RefreshToken):
    token_id: str
    family_id: str | None = None
    resource: str | None = None


class CustodianAuthorizationCode(AuthorizationCode):
    code_id: str
    code_hash: str


def ensure_oauth_schema() -> None:
    """Create OAuth tables if they do not already exist."""
    with db_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS oauth_clients (
                id TEXT PRIMARY KEY,
                name TEXT,
                secret TEXT,
                redirect_uris TEXT NOT NULL,
                token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
                grant_types TEXT NOT NULL,
                response_types TEXT NOT NULL,
                scope TEXT,
                client_uri TEXT,
                logo_uri TEXT,
                contacts TEXT,
                tos_uri TEXT,
                policy_uri TEXT,
                jwks_uri TEXT,
                jwks TEXT,
                software_id TEXT,
                software_version TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                client_id_issued_at INTEGER,
                secret_expires_at INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS oauth_tokens (
                id TEXT PRIMARY KEY,
                family_id TEXT,
                token_hash TEXT NOT NULL,
                token_type TEXT NOT NULL,
                client_id TEXT NOT NULL REFERENCES oauth_clients(id),
                scopes TEXT NOT NULL,
                resource TEXT,
                expires_at INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                revoked INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS oauth_auth_codes (
                code_hash TEXT PRIMARY KEY,
                code_id TEXT NOT NULL UNIQUE,
                client_id TEXT NOT NULL REFERENCES oauth_clients(id),
                redirect_uri TEXT NOT NULL,
                redirect_uri_provided_explicitly INTEGER DEFAULT 1,
                code_challenge TEXT NOT NULL,
                code_challenge_method TEXT NOT NULL,
                scopes TEXT NOT NULL,
                resource TEXT,
                expires_at INTEGER NOT NULL,
                used INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS oauth_approval_sessions (
                id TEXT PRIMARY KEY,
                client_id TEXT REFERENCES oauth_clients(id),
                expires_at INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_oauth_clients_name ON oauth_clients(name);
            CREATE INDEX IF NOT EXISTS idx_oauth_tokens_hash ON oauth_tokens(token_hash);
            CREATE INDEX IF NOT EXISTS idx_oauth_tokens_client_type ON oauth_tokens(client_id, token_type);
            CREATE INDEX IF NOT EXISTS idx_oauth_tokens_family ON oauth_tokens(family_id);
            CREATE INDEX IF NOT EXISTS idx_oauth_auth_codes_expires ON oauth_auth_codes(expires_at);
            """
        )
        conn.commit()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    return json.loads(value)


def _normalize_scopes(scopes: list[str] | None, fallback: list[str] | None = None) -> list[str]:
    if scopes:
        return list(scopes)
    if fallback:
        return list(fallback)
    return list(DEFAULT_SCOPES)


def _hash_value(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return bcrypt.hashpw(digest, bcrypt.gensalt()).decode("utf-8")


def _verify_hash(value: str, stored_hash: str) -> bool:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return bcrypt.checkpw(digest, stored_hash.encode("utf-8"))


def _build_secret_value(prefix: str) -> tuple[str, str, str]:
    record_id = uuid4().hex
    secret = secrets.token_hex(32)
    plain = f"{prefix}{record_id}.{secret}"
    return record_id, secret, plain


def _extract_token_id(prefix: str, value: str) -> str | None:
    if not value.startswith(prefix):
        return None
    token_id, sep, _secret = value[len(prefix) :].partition(".")
    if not sep or not token_id:
        return None
    return token_id


def _row_to_client(row) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=row["id"],
        client_secret=row["secret"],
        client_id_issued_at=row["client_id_issued_at"],
        client_secret_expires_at=row["secret_expires_at"],
        redirect_uris=_json_loads(row["redirect_uris"], []),
        token_endpoint_auth_method=row["token_endpoint_auth_method"],
        grant_types=_json_loads(row["grant_types"], ["authorization_code", "refresh_token"]),
        response_types=_json_loads(row["response_types"], ["code"]),
        scope=row["scope"],
        client_name=row["name"],
        client_uri=row["client_uri"],
        logo_uri=row["logo_uri"],
        contacts=_json_loads(row["contacts"], None),
        tos_uri=row["tos_uri"],
        policy_uri=row["policy_uri"],
        jwks_uri=row["jwks_uri"],
        jwks=_json_loads(row["jwks"], None),
        software_id=row["software_id"],
        software_version=row["software_version"],
    )


class CustodianOAuthProvider(
    OAuthAuthorizationServerProvider[
        CustodianAuthorizationCode,
        CustodianRefreshToken,
        CustodianAccessToken,
    ]
):
    def __init__(self, resource_server_url: str, default_scopes: list[str] | None = None):
        self.resource_server_url = resource_server_url
        self.default_scopes = default_scopes or list(DEFAULT_SCOPES)

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(ensure_oauth_schema)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await asyncio.to_thread(self._get_client_sync, client_id)

    def _get_client_sync(self, client_id: str) -> OAuthClientInformationFull | None:
        with db_connection() as conn:
            row = conn.execute("SELECT * FROM oauth_clients WHERE id = ?", (client_id,)).fetchone()
            if row is None:
                return None
            return _row_to_client(row)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await asyncio.to_thread(self._register_client_sync, client_info)

    def _register_client_sync(self, client_info: OAuthClientInformationFull) -> None:
        with db_connection() as conn:
            conn.execute(
                """
                INSERT INTO oauth_clients (
                    id, name, secret, redirect_uris, token_endpoint_auth_method,
                    grant_types, response_types, scope, client_uri, logo_uri,
                    contacts, tos_uri, policy_uri, jwks_uri, jwks,
                    software_id, software_version, client_id_issued_at, secret_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_info.client_id,
                    client_info.client_name,
                    client_info.client_secret,
                    _json_dumps([str(uri) for uri in client_info.redirect_uris or []]),
                    client_info.token_endpoint_auth_method or "none",
                    _json_dumps(list(client_info.grant_types or ["authorization_code", "refresh_token"])),
                    _json_dumps(list(client_info.response_types or ["code"])),
                    client_info.scope or " ".join(self.default_scopes),
                    str(client_info.client_uri) if client_info.client_uri else None,
                    str(client_info.logo_uri) if client_info.logo_uri else None,
                    _json_dumps(client_info.contacts) if client_info.contacts is not None else None,
                    str(client_info.tos_uri) if client_info.tos_uri else None,
                    str(client_info.policy_uri) if client_info.policy_uri else None,
                    str(client_info.jwks_uri) if client_info.jwks_uri else None,
                    _json_dumps(client_info.jwks) if client_info.jwks is not None else None,
                    client_info.software_id,
                    client_info.software_version,
                    client_info.client_id_issued_at or int(time.time()),
                    0 if client_info.client_secret else None,
                ),
            )
            conn.commit()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        return await asyncio.to_thread(self._authorize_sync, client, params)

    def _authorize_sync(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        scopes = _normalize_scopes(params.scopes, client.scope.split() if client.scope else self.default_scopes)
        code_id, _secret, plain_code = _build_secret_value(AUTH_CODE_PREFIX)
        code_hash = _hash_value(plain_code)
        expires_at = int(time.time()) + AUTH_CODE_TTL
        resource = params.resource or self.resource_server_url

        with db_connection() as conn:
            conn.execute(
                """
                INSERT INTO oauth_auth_codes (
                    code_hash, code_id, client_id, redirect_uri,
                    redirect_uri_provided_explicitly, code_challenge,
                    code_challenge_method, scopes, resource, expires_at, used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    code_hash,
                    code_id,
                    client.client_id,
                    str(params.redirect_uri),
                    1 if params.redirect_uri_provided_explicitly else 0,
                    params.code_challenge,
                    "S256",
                    _json_dumps(scopes),
                    resource,
                    expires_at,
                ),
            )
            conn.commit()

        return construct_redirect_uri(str(params.redirect_uri), code=plain_code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> CustodianAuthorizationCode | None:
        return await asyncio.to_thread(self._load_authorization_code_sync, client, authorization_code)

    def _load_authorization_code_sync(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> CustodianAuthorizationCode | None:
        code_id = _extract_token_id(AUTH_CODE_PREFIX, authorization_code)
        if code_id is None:
            return None

        with db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_auth_codes WHERE code_id = ? AND client_id = ? AND used = 0",
                (code_id, client.client_id),
            ).fetchone()
            if row is None:
                return None
            if not _verify_hash(authorization_code, row["code_hash"]):
                return None
            return CustodianAuthorizationCode(
                code=authorization_code,
                scopes=_json_loads(row["scopes"], []),
                expires_at=float(row["expires_at"]),
                client_id=row["client_id"],
                code_challenge=row["code_challenge"],
                redirect_uri=row["redirect_uri"],
                redirect_uri_provided_explicitly=bool(row["redirect_uri_provided_explicitly"]),
                resource=row["resource"],
                code_id=row["code_id"],
                code_hash=row["code_hash"],
            )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: CustodianAuthorizationCode
    ) -> OAuthToken:
        return await asyncio.to_thread(self._exchange_authorization_code_sync, client, authorization_code)

    def _exchange_authorization_code_sync(
        self, client: OAuthClientInformationFull, authorization_code: CustodianAuthorizationCode
    ) -> OAuthToken:
        with db_connection() as conn:
            result = conn.execute(
                "UPDATE oauth_auth_codes SET used = 1 WHERE code_hash = ? AND used = 0",
                (authorization_code.code_hash,),
            )
            if result.rowcount != 1:
                raise TokenError("invalid_grant", "authorization code has already been used")
            token = self._issue_token_pair_sync(
                conn,
                client_id=client.client_id or "",
                scopes=authorization_code.scopes,
                resource=authorization_code.resource,
            )
            conn.commit()
            return token

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> CustodianRefreshToken | None:
        return await asyncio.to_thread(self._load_refresh_token_sync, client, refresh_token)

    def _load_refresh_token_sync(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> CustodianRefreshToken | None:
        token_id = _extract_token_id(REFRESH_TOKEN_PREFIX, refresh_token)
        if token_id is None:
            return None

        with db_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM oauth_tokens
                WHERE id = ? AND token_type = 'refresh' AND revoked = 0 AND client_id = ?
                """,
                (token_id, client.client_id),
            ).fetchone()
            if row is None:
                return None
            if not _verify_hash(refresh_token, row["token_hash"]):
                return None
            return CustodianRefreshToken(
                token=refresh_token,
                client_id=row["client_id"],
                scopes=_json_loads(row["scopes"], []),
                expires_at=row["expires_at"],
                token_id=row["id"],
                family_id=row["family_id"],
                resource=row["resource"],
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: CustodianRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return await asyncio.to_thread(self._exchange_refresh_token_sync, client, refresh_token, scopes)

    def _exchange_refresh_token_sync(
        self,
        client: OAuthClientInformationFull,
        refresh_token: CustodianRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        requested_scopes = _normalize_scopes(scopes, refresh_token.scopes)
        if not set(requested_scopes).issubset(set(refresh_token.scopes)):
            raise TokenError("invalid_scope", "requested scope exceeds original grant")

        with db_connection() as conn:
            conn.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE family_id = ?",
                (refresh_token.family_id,),
            )
            token = self._issue_token_pair_sync(
                conn,
                client_id=client.client_id or "",
                scopes=requested_scopes,
                resource=refresh_token.resource,
            )
            conn.commit()
            return token

    async def load_access_token(self, token: str) -> CustodianAccessToken | None:
        return await asyncio.to_thread(self._load_access_token_sync, token)

    def _load_access_token_sync(self, token: str) -> CustodianAccessToken | None:
        token_id = _extract_token_id(ACCESS_TOKEN_PREFIX, token)
        if token_id is None:
            return None

        with db_connection() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_tokens WHERE id = ? AND token_type = 'access' AND revoked = 0",
                (token_id,),
            ).fetchone()
            if row is None:
                return None
            if not _verify_hash(token, row["token_hash"]):
                return None
            return CustodianAccessToken(
                token=token,
                client_id=row["client_id"],
                scopes=_json_loads(row["scopes"], []),
                expires_at=row["expires_at"],
                resource=row["resource"],
                token_id=row["id"],
                family_id=row["family_id"],
            )

    async def revoke_token(
        self,
        token: CustodianAccessToken | CustodianRefreshToken,
    ) -> None:
        await asyncio.to_thread(self._revoke_token_sync, token)

    def _revoke_token_sync(self, token: CustodianAccessToken | CustodianRefreshToken) -> None:
        family_id = getattr(token, "family_id", None)
        token_id = getattr(token, "token_id", None)
        with db_connection() as conn:
            if family_id:
                conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE family_id = ?", (family_id,))
            elif token_id:
                conn.execute("UPDATE oauth_tokens SET revoked = 1 WHERE id = ?", (token_id,))
            conn.commit()

    def _issue_token_pair_sync(
        self,
        conn,
        *,
        client_id: str,
        scopes: list[str],
        resource: str | None,
    ) -> OAuthToken:
        family_id = uuid4().hex
        access_id, _access_secret, access_token = _build_secret_value(ACCESS_TOKEN_PREFIX)
        refresh_id, _refresh_secret, refresh_token = _build_secret_value(REFRESH_TOKEN_PREFIX)
        now = int(time.time())
        expires_at = now + ACCESS_TOKEN_TTL
        scoped_resource = resource or self.resource_server_url

        conn.execute(
            """
            INSERT INTO oauth_tokens (
                id, family_id, token_hash, token_type, client_id,
                scopes, resource, expires_at, revoked
            ) VALUES (?, ?, ?, 'access', ?, ?, ?, ?, 0)
            """,
            (
                access_id,
                family_id,
                _hash_value(access_token),
                client_id,
                _json_dumps(scopes),
                scoped_resource,
                expires_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO oauth_tokens (
                id, family_id, token_hash, token_type, client_id,
                scopes, resource, expires_at, revoked
            ) VALUES (?, ?, ?, 'refresh', ?, ?, ?, NULL, 0)
            """,
            (
                refresh_id,
                family_id,
                _hash_value(refresh_token),
                client_id,
                _json_dumps(scopes),
                scoped_resource,
            ),
        )
        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes),
        )


async def get_named_client(name: str) -> dict[str, Any] | None:
    return await asyncio.to_thread(_get_named_client_sync, name)


def _get_named_client_sync(name: str) -> dict[str, Any] | None:
    ensure_oauth_schema()
    with db_connection() as conn:
        row = conn.execute(
            "SELECT id, name, created_at, secret FROM oauth_clients WHERE name = ? ORDER BY created_at DESC LIMIT 1",
            (name,),
        ).fetchone()
        return dict(row) if row is not None else None


async def create_or_rotate_named_client(
    name: str,
    *,
    rotate_secret: bool = False,
    redirect_uris: list[str] | None = None,
) -> tuple[str, str | None, bool, str]:
    return await asyncio.to_thread(
        _create_or_rotate_named_client_sync,
        name,
        rotate_secret,
        redirect_uris,
    )


def _create_or_rotate_named_client_sync(
    name: str,
    rotate_secret: bool,
    redirect_uris: list[str] | None,
) -> tuple[str, str | None, bool, str]:
    ensure_oauth_schema()
    now = int(time.time())
    normalized_redirects = list(redirect_uris or [])
    with db_connection() as conn:
        row = conn.execute(
            "SELECT id, created_at FROM oauth_clients WHERE name = ? ORDER BY created_at DESC LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            client_id = uuid4().hex
            client_secret = secrets.token_hex(32)
            conn.execute(
                """
                INSERT INTO oauth_clients (
                    id, name, secret, redirect_uris, token_endpoint_auth_method,
                    grant_types, response_types, scope, client_id_issued_at, secret_expires_at
                ) VALUES (?, ?, ?, ?, 'client_secret_post', ?, ?, ?, ?, 0)
                """,
                (
                    client_id,
                    name,
                    client_secret,
                    _json_dumps(normalized_redirects),
                    _json_dumps(["authorization_code", "refresh_token"]),
                    _json_dumps(["code"]),
                    " ".join(DEFAULT_SCOPES),
                    now,
                ),
            )
            conn.commit()
            return client_id, client_secret, True, "created"

        client_id = row["id"]
        created_at = row["created_at"]
        if not rotate_secret:
            return client_id, None, False, created_at

        client_secret = secrets.token_hex(32)
        if redirect_uris is None:
            conn.execute(
                "UPDATE oauth_clients SET secret = ?, secret_expires_at = 0 WHERE id = ?",
                (client_secret, client_id),
            )
        else:
            conn.execute(
                "UPDATE oauth_clients SET secret = ?, redirect_uris = ?, secret_expires_at = 0 WHERE id = ?",
                (client_secret, _json_dumps(normalized_redirects), client_id),
            )
        conn.commit()
        return client_id, client_secret, False, "rotated"
