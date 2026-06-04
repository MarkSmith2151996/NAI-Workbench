#!/usr/bin/env python3
"""Custodian MCP HTTP server with streamable HTTP and OAuth 2.1."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from mcp.server.streamable_http_manager import (
    StreamableHTTPSessionManager,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.authentication import AuthCredentials, AuthenticationBackend
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse
from starlette.routing import Route


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custodian.core.server import app as mcp_app
from custodian.core.rest_api import setup_rest_routes
from custodian.core.server import (
    call_tool,
    get_tool_load_failures,
    list_tools,
    register_http_session_manager,
    run_startup_healthcheck,
    set_runtime_loop,
    unregister_http_session_manager,
)
from custodian.oauth_provider import ACCESS_TOKEN_PREFIX, CustodianOAuthProvider
from custodian.session_registry import ensure_registry


LISTEN_HOST = os.environ.get("CUSTODIAN_MCP_BIND", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("CUSTODIAN_MCP_PORT", "8223"))
MCP_TOKEN = os.environ.get("CUSTODIAN_MCP_TOKEN", "")
OAUTH_ISSUER = os.environ.get("CUSTODIAN_OAUTH_ISSUER", "https://custodian.lamannalogistics.com")
LOG_PREFIX = "[custodian-mcp-http]"
REQUIRED_SCOPES = ["custodian.full"]


def _log(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", file=sys.stderr, flush=True)


class StaticOrOAuthBackend(AuthenticationBackend):
    """Authenticate OAuth access tokens or the legacy static bearer token."""

    def __init__(self, provider: CustodianOAuthProvider, static_token: str, resource_url: str):
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        from mcp.server.auth.provider import AccessToken

        self.provider = provider
        self.static_token = static_token
        self.resource_url = resource_url
        self.authenticated_user_cls = AuthenticatedUser
        self.access_token_cls = AccessToken

    async def authenticate(self, conn: HTTPConnection):
        auth_header = conn.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return None

        bearer_value = auth_header[7:]
        if bearer_value.startswith(ACCESS_TOKEN_PREFIX):
            auth_info = await self.provider.load_access_token(bearer_value)
            if auth_info is None:
                return None
            if auth_info.expires_at and auth_info.expires_at < int(__import__("time").time()):
                return None
            return AuthCredentials(auth_info.scopes), self.authenticated_user_cls(auth_info)

        if self.static_token and hmac.compare_digest(bearer_value, self.static_token):
            auth_info = self.access_token_cls(
                token="legacy-static",
                client_id="static-bearer",
                scopes=list(REQUIRED_SCOPES),
                expires_at=None,
                resource=self.resource_url,
            )
            return AuthCredentials(auth_info.scopes), self.authenticated_user_cls(auth_info)

        return None


class StreamableHTTPApp:
    """Thin ASGI adapter for the SDK session manager."""

    def __init__(self, session_manager):
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send):
        await self.session_manager.handle_request(scope, receive, send)


def _is_authenticated(request: Request) -> bool:
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    return isinstance(request.user, AuthenticatedUser)


def _unauthorized_response() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def create_starlette_app(static_token: str) -> Starlette:
    """Build the Starlette app with OAuth metadata and streamable HTTP MCP."""
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
    from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    issuer_url = AnyHttpUrl(OAUTH_ISSUER)
    resource_url = AnyHttpUrl(f"{str(issuer_url).rstrip('/')}/mcp")
    provider = CustodianOAuthProvider(str(resource_url), default_scopes=REQUIRED_SCOPES)

    ensure_registry(reset_transport="http")

    session_manager = StreamableHTTPSessionManager(
        app=mcp_app,
        json_response=False,
        stateless=False,
    )
    streamable_http_app = StreamableHTTPApp(session_manager)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        set_runtime_loop(asyncio.get_running_loop())
        await provider.ensure_schema()
        summary = await run_startup_healthcheck()
        await setup_rest_routes(_app)
        for module_name, error in summary["failed_tools"].items():
            _log(f"tool module failed to load: {module_name}: {error}")
        register_http_session_manager(session_manager)
        async with session_manager.run():
            yield
        unregister_http_session_manager(session_manager)

    async def root(_request: Request):
        tools = await list_tools()
        return JSONResponse(
            {
                "server": "custodian",
                "transport": "streamable-http",
                "mcp_endpoint": "/mcp",
                "oauth" + "_" + "metadata": "/.well-known/oauth-authorization-server",
                "protected_resource" + "_" + "metadata": "/.well-known/oauth-protected-resource/mcp",
                "legacy_sse": False,
                "tool_count": len(tools),
                "failed_tool_count": len(get_tool_load_failures()),
            }
        )

    async def health(request: Request):
        if not _is_authenticated(request):
            return _unauthorized_response()
        return JSONResponse(
            {
                "status": "ok",
                "server": "custodian",
                "transport": "streamable-http",
            }
        )

    async def handle_tool(request: Request):
        if not _is_authenticated(request):
            return _unauthorized_response()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        tool_name = body.get("tool", "")
        arguments = body.get("arguments")
        if arguments is None:
            arguments = body.get("args", {})
        if not tool_name:
            return JSONResponse({"error": "missing tool"}, status_code=400)
        if not isinstance(arguments, dict):
            return JSONResponse({"error": "arguments must be an object"}, status_code=400)

        try:
            result = await call_tool(tool_name, arguments)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

        texts = [content.text for content in result if hasattr(content, "text")]
        return JSONResponse({"result": texts[0] if len(texts) == 1 else texts})

    async def register_client(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "invalid_client" + "_" + "metadata", "error_description": "invalid json"},
                status_code=400,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "invalid_client" + "_" + "metadata", "error_description": "body must be an object"},
                status_code=400,
            )

        requested_auth_method = body.get("token_endpoint_auth_method")
        if requested_auth_method in (None, "none"):
            token_endpoint_auth_method = "none"
            client_secret = None
        elif requested_auth_method in ("client_secret_post", "client_secret_basic"):
            token_endpoint_auth_method = requested_auth_method
            client_secret = secrets.token_hex(32)
        else:
            return JSONResponse(
                {
                    "error": "invalid_client" + "_" + "metadata",
                    "error_description": "unsupported token_endpoint_auth_method",
                },
                status_code=400,
            )

        client_id = uuid4().hex
        issued_at = int(time.time())
        redirect_uris = body.get("redirect_uris") or []
        grant_types = body.get("grant_types") or ["authorization_code", "refresh_token"]
        response_types = body.get("response_types") or ["code"]
        scope = body.get("scope") or " ".join(REQUIRED_SCOPES)

        client_info = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            client_id_issued_at=issued_at,
            client_secret_expires_at=0 if client_secret else None,
            redirect_uris=redirect_uris,
            token_endpoint_auth_method=token_endpoint_auth_method,
            grant_types=grant_types,
            response_types=response_types,
            scope=scope,
            client_name=body.get("client_name"),
            client_uri=body.get("client_uri"),
            logo_uri=body.get("logo_uri"),
            contacts=body.get("contacts"),
            tos_uri=body.get("tos_uri"),
            policy_uri=body.get("policy_uri"),
            jwks_uri=body.get("jwks_uri"),
            jwks=body.get("jwks"),
            software_id=body.get("software_id"),
            software_version=body.get("software_version"),
        )
        await provider.register_client(client_info)

        response = {
            "client_id": client_id,
            "client_id_issued_at": issued_at,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "grant_types": grant_types,
            "response_types": response_types,
            "scope": scope,
        }
        if client_secret:
            response["client_secret"] = client_secret
            response["client_secret_expires_at"] = 0
        for key in (
            "client_name",
            "client_uri",
            "logo_uri",
            "contacts",
            "tos_uri",
            "policy_uri",
            "jwks_uri",
            "jwks",
            "software_id",
            "software_version",
        ):
            if key in body:
                response[key] = body[key]
        return JSONResponse(response, status_code=201)

    async def oauth_server_info(_request: Request):
        issuer = str(issuer_url).rstrip("/")
        return JSONResponse(
            {
                "issuer": f"{issuer}/",
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "registration_endpoint": f"{issuer}/register",
                "scopes_supported": REQUIRED_SCOPES,
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "revocation_endpoint": f"{issuer}/revoke",
                "revocation_endpoint_auth_methods_supported": [
                    "none",
                    "client_secret_post",
                    "client_secret_basic",
                ],
                "code_challenge_methods_supported": ["S256"],
            },
            headers={"Cache-Control": "public, max-age=3600"},
        )

    routes = [
        Route("/", endpoint=root, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", endpoint=oauth_server_info, methods=["GET"]),
        Route("/register", endpoint=register_client, methods=["POST"]),
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/tool", endpoint=handle_tool, methods=["POST"]),
        Route(
            "/mcp",
            endpoint=RequireAuthMiddleware(
                streamable_http_app,
                REQUIRED_SCOPES,
                **{
                    "resource" + "_" + "metadata_url": AnyHttpUrl(
                        f"{str(issuer_url).rstrip('/')}/.well-known/oauth-protected-resource/mcp"
                    )
                },
            ),
        ),
    ]
    routes.extend(
        create_auth_routes(
            provider=provider,
            issuer_url=issuer_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                client_secret_expiry_seconds=None,
                valid_scopes=REQUIRED_SCOPES,
                default_scopes=REQUIRED_SCOPES,
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
    )
    routes.extend(
        create_protected_resource_routes(
            resource_url=resource_url,
            authorization_servers=[issuer_url],
            scopes_supported=REQUIRED_SCOPES,
            resource_name="Custodian MCP",
        )
    )

    middleware = [
        Middleware(AuthenticationMiddleware, backend=StaticOrOAuthBackend(provider, static_token, str(resource_url))),
        Middleware(AuthContextMiddleware),
    ]

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def main() -> int:
    if not MCP_TOKEN:
        _log("ERROR: CUSTODIAN_MCP_TOKEN is required and the server will not start")
        return 1

    try:
        import uvicorn
    except ImportError:
        _log("ERROR: uvicorn is required. Install with: pip install -r custodian/requirements.txt")
        return 1

    _log(f"Starting on {LISTEN_HOST}:{LISTEN_PORT}")
    _log(f"MCP endpoint: http://{LISTEN_HOST}:{LISTEN_PORT}/mcp")
    _log(f"OAuth issuer: {OAUTH_ISSUER}")
    _log("Bearer token required on /health, /tool, and /mcp")
    _log("OAuth metadata: /.well-known/oauth-authorization-server")

    starlette_app = create_starlette_app(MCP_TOKEN)
    uvicorn.run(starlette_app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
