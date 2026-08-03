"""SurfSense MCP Server implementation."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("fastmcp.surfsense_mcp")

from fastmcp import FastMCP
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from mcp.types import Icon

from surfsense_mcp.tools import register_tools

ICON = Icon(src="https://surfsense.net/favicon.ico", alt="SurfSense MCP Server")

_INSTRUCTIONS = (
    "Manages search spaces, documents, research threads, reports, notes, and "
    "logs in SurfSense, an AI research platform.\n\n"
    "Every available tool is listed up front — call them directly. There is no "
    "discovery or enablement step.\n\n"
    "Getting started: use list_search_spaces to discover available search "
    "spaces, then search_documents or query_surfsense to find and interact "
    "with content."
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


def _allowed_client_redirect_uris() -> list[str] | None:
    """Parse MCP_ALLOWED_CLIENT_REDIRECT_URIS (comma-separated).

    Unset/empty → None (allow all), matching penpot-mcp's behaviour so SMBs
    can deploy with any MCP client without pre-configuring callback URLs.
    """
    raw = os.getenv("MCP_ALLOWED_CLIENT_REDIRECT_URIS", "").strip()
    if not raw:
        logger.warning(
            "MCP_ALLOWED_CLIENT_REDIRECT_URIS is unset — dynamic client registration "
            "accepts any redirect_uri. Set an allow-list for hardened deployments."
        )
        return None

    allowed_uris = [uri.strip() for uri in raw.split(",") if uri.strip()]
    return allowed_uris or None


def _required_scopes() -> list[str]:
    """OAuth scopes requested upstream from Cognito (space- or comma-separated).

    Default is ``openid`` only. Cognito already includes the user's readable
    attributes — crucially ``email`` and ``cognito:username`` — in the **id_token**
    of the authorization-code flow with just ``openid``, which is all the SurfSense
    identity relay needs (see ``auth/cognito.py``). Requesting ``email`` / ``profile``
    additionally is **not** required and fails with ``invalid_scope`` unless those
    OAuth scopes are explicitly enabled on the Cognito app client. Operators whose
    client does enable them can opt in via ``MCP_OIDC_SCOPES="openid email profile"``.
    """
    raw = os.getenv("MCP_OIDC_SCOPES", "").strip()
    if not raw:
        return ["openid"]
    scopes = [s.strip() for s in raw.replace(",", " ").split() if s.strip()]
    return scopes or ["openid"]


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

    * HTTPS base URL → forward the upstream id_token through Traefik+mPass and
      let oauth2-proxy validate it (against the same JWKS) and set
      ``X-Auth-Request-Email`` / ``X-Auth-Request-User`` itself.
    * HTTP base URL → call SurfSense direct on the docker network and inject
      ``X-Auth-Request-Email`` from the id_token's ``email`` claim.

    Both paths rely on :class:`~surfsense_mcp.auth.cognito.SurfSenseCognitoProvider`
    to surface the id_token's identity claims — the Cognito *access* token alone
    carries no ``email`` and an opaque ``username`` for federated users, which
    would resolve a different SurfSense account than the web login.
    """
    from surfsense_mcp.auth.cognito import SurfSenseCognitoProvider
    from surfsense_mcp.auth.storage import build_oauth_storage

    client_secret = os.getenv("OIDC_CLIENT_SECRET", "")
    jwt_signing_key = os.getenv("MCP_JWT_SIGNING_KEY") or None

    provider = SurfSenseCognitoProvider(
        user_pool_id=os.environ["COGNITO_USER_POOL_ID"],
        aws_region=os.environ["COGNITO_AWS_REGION"],
        client_id=os.environ["OIDC_CLIENT_ID"],
        client_secret=client_secret,
        base_url=os.environ["MCP_BASE_URL"],
        redirect_path="/auth/callback",
        # `openid` only by default — Cognito puts email / cognito:username in the
        # id_token without the email/profile OAuth scopes, and requesting those
        # against a client that doesn't enable them fails with invalid_scope.
        # Override via MCP_OIDC_SCOPES if the app client allows more (see helper).
        required_scopes=_required_scopes(),
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

    # AWSCognitoProvider fetches the authorization_endpoint from Cognito's OIDC
    # discovery, which points at Cognito's hosted UI. In deployments where an
    # auth proxy (e.g. mpass-auth-proxy) sits in front of Cognito and provides
    # the actual login page, override the authorize redirect so users land on
    # the proxy instead of raw Cognito.
    upstream_auth_url = os.getenv("COGNITO_UPSTREAM_AUTH_URL", "").strip()
    if upstream_auth_url:
        provider._upstream_authorization_endpoint = upstream_auth_url
    upstream_token_url = os.getenv("COGNITO_UPSTREAM_TOKEN_URL", "").strip()
    if upstream_token_url:
        provider._upstream_token_endpoint = upstream_token_url

    mcp = FastMCP(
        "SurfSense MCP Server (http)",
        instructions=_INSTRUCTIONS,
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
    mcp = FastMCP("SurfSense MCP Server (stdio)", instructions=_INSTRUCTIONS, icons=[ICON])
    mcp.add_middleware(StructuredLoggingMiddleware(include_payloads=_log_payloads_enabled()))
    register_tools(mcp)
    return mcp
