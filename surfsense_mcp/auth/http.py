"""HTTP-mode auth: SurfSense-specific dispatch on ``SURFSENSE_BASE_URL`` scheme.

The headers themselves (``Authorization: Bearer …`` vs
``X-Auth-Request-User: …``) and the FastMCP ``get_access_token()``
swallow-RuntimeError dance are generic — they live in
:mod:`moneta_mcp_auth.identity`. This shim just reads
``SURFSENSE_BASE_URL`` to pick which strategy to use and re-exports the
generic helpers so existing call sites (``from surfsense_mcp.auth.http
import auth_headers_for_token``) keep working.
"""

from __future__ import annotations

import os

from fastmcp.server.auth.auth import AccessToken
from moneta_mcp_auth.identity import auth_headers_for_token as _generic_auth_headers_for_token
from moneta_mcp_auth.identity import bearer_header, request_token, username_header

__all__ = [
    "auth_headers_for_token",
    "bearer_header",
    "request_token",
    "username_header",
]


def auth_headers_for_token(token: AccessToken) -> dict[str, str]:
    """Pick the auth strategy based on whether the call goes through mPass.

    Reads ``SURFSENSE_BASE_URL`` inline — operators who need a different
    rule can override here rather than juggling another env var. HTTPS
    base URL → forward the validated Cognito Bearer untouched (oauth2-proxy
    is the verifier on the way through Traefik). HTTP base URL → inject
    ``X-Auth-Request-User`` ourselves (direct docker-network call).
    """
    upstream_is_https = os.getenv("SURFSENSE_BASE_URL", "").startswith("https://")
    return _generic_auth_headers_for_token(token, upstream_is_https=upstream_is_https)
