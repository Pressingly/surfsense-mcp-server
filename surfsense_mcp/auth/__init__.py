"""Per-request auth headers for SurfSense backend calls.

Two transports, three strategies (the HTTP transport branches on URL scheme):

- **stdio** (single-user, on a developer's laptop) → ``Authorization: Bearer
  <surfsense-jwt>`` from ``SURFSENSE_JWT`` or the password fallback.
- **http + HTTPS base URL** (call traverses Traefik + mPass) →
  ``Authorization: Bearer <cognito-id-token>``; oauth2-proxy validates the JWT
  against the Cognito JWKS and sets ``X-Auth-Request-Email`` itself. The
  id_token (not the access token) is forwarded because only it carries the
  ``email`` / ``cognito:username`` claims oauth2-proxy needs.
- **http + HTTP base URL** (direct docker-network call, no Traefik) →
  ``X-Auth-Request-Email`` derived from the id_token's ``email`` claim. The
  Cognito Bearer is *not* forwarded.

The dispatcher in :func:`build_auth_headers` picks based on whether a FastMCP
HTTP request scope is active; the per-mode internals live in
:mod:`surfsense_mcp.auth.stdio` and :mod:`surfsense_mcp.auth.http` (the latter
also owns the HTTPS-vs-HTTP scheme branch).
"""

from __future__ import annotations

from surfsense_mcp.auth import http as _http
from surfsense_mcp.auth import stdio as _stdio

__all__ = [
    "auth_came_from_password",
    "build_auth_headers",
    "invalidate_password_token",
]


async def build_auth_headers() -> dict[str, str]:
    """Return the auth header(s) to attach to the next SurfSense backend call."""
    token = _http.request_token()
    if token is not None:
        return _http.auth_headers_for_token(token)
    return {"Authorization": f"Bearer {await _stdio.resolve_jwt()}"}


def auth_came_from_password() -> bool:
    """Gate the 401-retry-once path.

    HTTP mode: never retry — a 401 means the Cognito token validated fine but
    the backend rejected the user (real provisioning failure), and we want to
    surface that. Stdio with a paste JWT: never retry — the user pastes a new
    one. Stdio with password fallback: retry — the cached token may have
    expired server-side before our local TTL.
    """
    if _http.request_token() is not None:
        return False
    return _stdio.is_password_in_use()


def invalidate_password_token() -> None:
    """Drop the stdio password-cache so the next call forces a fresh login."""
    _stdio.invalidate_cache()
