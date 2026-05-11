"""SurfSense MCP Server implementation."""

from __future__ import annotations

import os

from fastmcp import FastMCP
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from mcp.types import Icon
from moneta_mcp_auth.redirects import parse_allowed_client_redirect_uris

from surfsense_mcp.tools import register_tools

ICON = Icon(src="https://surfsense.net/favicon.ico", alt="SurfSense MCP Server")

# Default — covers Claude Desktop, Cursor, and MCP Inspector on a developer
# laptop. Non-localhost MCP clients (e.g. the Askii AI app) must be added via
# MCP_ALLOWED_CLIENT_REDIRECT_URIS to register at /register (DCR shim).
_DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS: tuple[str, ...] = (
    "http://localhost:*/*",
    "http://127.0.0.1:*/*",
)

_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _log_payloads_enabled() -> bool:
    """Whether the structured-logging middleware should include tool payloads.

    Default is False because tool inputs/outputs include chat prompts,
    document bodies, and base64-encoded uploads (up to 500 MB) — logging
    those by default would leak sensitive user data and balloon log
    volume. Operators can opt in via MCP_LOG_PAYLOADS=1 for short
    debugging windows.
    """
    return os.getenv("MCP_LOG_PAYLOADS", "").strip().lower() in _TRUTHY_ENV_VALUES


def _allowed_client_redirect_uris() -> list[str]:
    """Parse MCP_ALLOWED_CLIENT_REDIRECT_URIS with surfsense's localhost defaults.

    Unset/empty → localhost defaults. Operators opt in to broader allow-lists
    by listing specific patterns — do not allow arbitrary redirect URIs, they
    let an attacker DCR-register a malicious client and exfiltrate user tokens
    once the user authorizes.
    """
    # parse_allowed_client_redirect_uris returns ``list[str]`` whenever
    # ``defaults`` is non-empty — surfsense always passes localhost defaults,
    # so the ``None`` branch is unreachable here.
    return parse_allowed_client_redirect_uris(defaults=_DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS) or []


def get_header_mcp() -> FastMCP:
    """HTTP mode — FastMCP is the sole auth layer (mPass is NOT in front).

    ``AWSCognitoProvider`` makes this service a full OAuth 2.0 authorization
    server (via OAuthProxy / OIDCProxy). It publishes RFC 7591 + RFC 8414 +
    RFC 9728 discovery, implements the ``/register`` DCR shim backed by the
    pre-registered Cognito app client, and proxies ``/authorize`` /
    ``/auth/callback`` / ``/token`` to Cognito. MCP clients (Claude Desktop,
    Cursor) discover all of this automatically and run the OAuth flow without
    any manual Bearer paste.

    The same Cognito access token the client obtains is then Bearer-validated
    via Cognito's JWKS on every request to ``/mcp``; how that identity reaches
    the SurfSense backend depends on ``SURFSENSE_BASE_URL`` (see
    :mod:`surfsense_mcp.auth.http`):

    * HTTPS base URL → forward the validated Cognito Bearer untouched and let
      oauth2-proxy / mPass validate it (against the same JWKS) and set
      ``X-Auth-Request-User`` itself.
    * HTTP base URL → call SurfSense direct on the docker network and inject
      ``X-Auth-Request-User`` from the validated token's ``username`` claim.
    """
    from fastmcp.server.auth.providers.aws import AWSCognitoProvider

    # Imported via the local re-export module so tests that monkeypatch
    # ``surfsense_mcp.auth.storage.build_oauth_storage`` still intercept this
    # call site. The actual implementation lives in ``moneta_mcp_auth.storage``.
    from surfsense_mcp.auth.storage import build_oauth_storage

    client_secret = os.getenv("OIDC_CLIENT_SECRET", "")
    jwt_signing_key = os.getenv("MCP_JWT_SIGNING_KEY") or None

    provider = AWSCognitoProvider(
        user_pool_id=os.environ["COGNITO_USER_POOL_ID"],
        aws_region=os.environ["COGNITO_AWS_REGION"],
        client_id=os.environ["OIDC_CLIENT_ID"],
        client_secret=client_secret,
        base_url=os.environ["MCP_BASE_URL"],
        redirect_path="/auth/callback",
        required_scopes=["openid"],
        allowed_client_redirect_uris=_allowed_client_redirect_uris(),
        # Cognito User Pools don't honor RFC 8707 Resource Indicators the way
        # the spec requires — forwarding `resource` on /authorize without it
        # being echoed on /token causes Cognito to return invalid_grant on the
        # token exchange. MCP clients still send `resource` to FastMCP; we just
        # don't pass it through to the upstream IdP.
        forward_resource=False,
        # None → FastMCP keeps its encrypted-file default. See auth/storage.py.
        client_storage=build_oauth_storage(),
        # For confidential clients FastMCP derives the JWT signing key from
        # client_secret automatically. Public clients have no secret, so the
        # entropy must come from MCP_JWT_SIGNING_KEY instead.
        jwt_signing_key=jwt_signing_key,
    )

    mcp = FastMCP(
        "SurfSense MCP Server (http)",
        icons=[ICON],
        website_url="https://surfsense.net",
        auth=provider,
    )
    mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=_log_payloads_enabled()))
    register_tools(mcp)
    return mcp


def get_stdio_mcp() -> FastMCP:
    """Stdio mode — supports two upstream auth paths.

    It can use the ``SURFSENSE_JWT`` environment variable for upstream auth,
    or fall back to the email/password login flow provided by
    ``surfsense_mcp.auth.stdio``.
    """
    mcp = FastMCP("SurfSense MCP Server (stdio)", icons=[ICON])
    mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=_log_payloads_enabled()))
    register_tools(mcp)
    return mcp
