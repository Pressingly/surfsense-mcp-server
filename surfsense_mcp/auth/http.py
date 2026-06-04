"""HTTP-mode auth: dispatch identity-header vs Cognito-Bearer by URL scheme.

In HTTP mode FastMCP's ``AWSCognitoProvider`` validates the inbound Bearer
against the Cognito user pool's JWKS and exposes the filtered claims via
``get_access_token()``. There are two supported ways to relay that identity
to the SurfSense backend, and the right one depends on whether the request
goes through Traefik:

* **HTTPS base URL** (e.g. ``https://foss-research.local.moneta.dev``) —
  request traverses Traefik + mPass. Traefik strips ``X-Auth-Request-*``
  headers, so we forward a Cognito Bearer and let oauth2-proxy validate it
  (against the same JWKS) and set ``X-Auth-Request-User`` /
  ``X-Auth-Request-Email`` itself before the request reaches SurfSense.
* **HTTP base URL** (e.g. ``http://surfsense-backend:8000``) — direct on
  the docker network with no Traefik in the path. We inject the identity
  header ourselves; SurfSense's ``ProxyAuthMiddleware`` reads it and
  auto-provisions the user.

**The Bearer we forward is the id_token, not the access token.** A Cognito
*access* token carries no ``email`` claim and, for users federated from an
external IdP, its ``username`` is an opaque Cognito UUID rather than the human
identifier the browser login keys on — forwarding it makes oauth2-proxy fall
back to ``sub`` and provision a *different* user than the web session. The
id_token carries ``email`` and ``cognito:username`` exactly like the cookie
flow. :class:`surfsense_mcp.auth.cognito.SurfSenseCognitoProvider` decodes the
id_token at token-exchange time and stashes those values (plus the raw
id_token) under ``AccessToken.claims["upstream_claims"]``; the helpers below
read them from there.

The dispatcher in :func:`auth_headers_for_token` reads ``SURFSENSE_BASE_URL``
inline rather than threading it through call signatures — keeps the public
shape of :mod:`surfsense_mcp.auth` (``build_auth_headers() -> dict``)
unchanged.
"""

from __future__ import annotations

import os
from typing import Any

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.dependencies import get_access_token
from fastmcp.utilities.logging import get_logger

logger = get_logger(__name__)

# Key under which SurfSenseCognitoProvider embeds id-token-derived identity in
# the FastMCP-signed reference JWT (see fastmcp OAuthProxy._extract_upstream_claims).
UPSTREAM_CLAIMS_KEY = "upstream_claims"
# Sub-keys inside that dict.
ID_TOKEN_KEY = "id_token"
EMAIL_CLAIM = "email"
COGNITO_USERNAME_CLAIM = "cognito:username"


def request_token() -> AccessToken | None:
    """Return the FastMCP-validated access token for the in-flight HTTP request.

    Returns ``None`` outside an HTTP request scope (i.e. in stdio mode), where
    ``get_access_token()`` raises ``RuntimeError`` because there's no auth
    context to read from. The dispatcher in ``surfsense_mcp.auth`` uses the
    ``None`` return as the signal to fall back to stdio JWT resolution.
    """
    try:
        return get_access_token()
    except RuntimeError:
        return None


def upstream_claims(token: AccessToken) -> dict[str, Any]:
    """Return the id-token-derived claims embedded by ``SurfSenseCognitoProvider``.

    Empty dict when absent — stdio mode, or a token issued before this provider
    was deployed — so callers can fall back to the access token's own claims.
    """
    claims = token.claims or {}
    upstream = claims.get(UPSTREAM_CLAIMS_KEY)
    return upstream if isinstance(upstream, dict) else {}


def identity_header(token: AccessToken) -> dict[str, str]:
    """Build the identity header for the trusted internal-DNS (HTTP) path.

    Resolution order, most-to-least authoritative:

    1. id_token ``email`` → ``X-Auth-Request-Email``. This is what SurfSense's
       ``ProxyAuthMiddleware`` reads first and what oauth2-proxy sends on the
       browser flow, so the MCP user resolves to the *same* account as the web
       login (e.g. ``1020010000020127@askii.ai``).
    2. id_token ``cognito:username`` → ``X-Auth-Request-User`` (SurfSense
       synthesizes ``{username}@{domain}``).
    3. access-token ``username`` → ``X-Auth-Request-User``. Correct for native
       Cognito users (where ``username`` is the human handle); for federated
       users this is the opaque UUID, but it's only reached when no id_token was
       captured, which shouldn't happen with ``SurfSenseCognitoProvider``.

    Raises ``RuntimeError`` if none yields a usable identity — we fail fast
    rather than send an identity-less request that the backend would
    auto-provision under a bogus (anonymous-ish) user.
    """
    upstream = upstream_claims(token)

    email = upstream.get(EMAIL_CLAIM)
    if isinstance(email, str) and email:
        return {"X-Auth-Request-Email": email}

    cognito_username = upstream.get(COGNITO_USERNAME_CLAIM)
    if isinstance(cognito_username, str) and cognito_username:
        return {"X-Auth-Request-User": cognito_username}

    claims = token.claims or {}
    username = claims.get("username")
    if isinstance(username, str) and username:
        return {"X-Auth-Request-User": username}

    raise RuntimeError(
        "No email or username claim on the validated token — cannot identify "
        "the upstream SurfSense user. Check that SurfSenseCognitoProvider is in "
        "use and the Cognito id_token carries an email / cognito:username claim."
    )


def bearer_header(token: AccessToken) -> dict[str, str]:
    """Forward a Cognito Bearer to oauth2-proxy / mPass on the public-URL path.

    Traefik's ``strip-auth-headers`` middleware removes any ``X-Auth-Request-*``
    we might inject, so identity has to ride the standard ``Authorization``
    header for oauth2-proxy to pick up.

    We forward the **id_token** (stashed under ``upstream_claims`` by
    ``SurfSenseCognitoProvider``), because oauth2-proxy derives identity from
    the ``email`` / ``cognito:username`` claims and a Cognito access token
    carries neither. oauth2-proxy validates the id_token against the same
    Cognito JWKS (``aud`` matches the client id — accepted via
    ``OAUTH2_PROXY_OIDC_AUDIENCE_CLAIMS=aud,client_id``), identical to the
    browser cookie flow.

    Falls back to the raw access token only when no id_token is available; logs
    a warning because that path is known to misidentify federated users.
    """
    id_token = upstream_claims(token).get(ID_TOKEN_KEY)
    if isinstance(id_token, str) and id_token:
        return {"Authorization": f"Bearer {id_token}"}

    raw = token.token
    if not raw:
        raise RuntimeError(
            "Validated AccessToken has no upstream id_token and no raw token "
            "string — cannot forward identity to oauth2-proxy. Ensure "
            "SurfSenseCognitoProvider is in use."
        )
    logger.warning(
        "No upstream id_token on the validated token — forwarding the Cognito "
        "access token to oauth2-proxy instead. oauth2-proxy will fall back to "
        "the `sub` claim and may resolve the wrong SurfSense user. Ensure "
        "SurfSenseCognitoProvider is in use."
    )
    return {"Authorization": f"Bearer {raw}"}


def auth_headers_for_token(token: AccessToken) -> dict[str, str]:
    """Pick the auth strategy based on whether the call goes through mPass.

    Reads ``SURFSENSE_BASE_URL`` inline. The check is intentionally simple
    (scheme prefix) — operators who need a different rule can override
    explicitly by editing this function rather than juggling another env var.
    """
    base_url = os.getenv("SURFSENSE_BASE_URL", "")
    if base_url.startswith("https://"):
        return bearer_header(token)
    return identity_header(token)
