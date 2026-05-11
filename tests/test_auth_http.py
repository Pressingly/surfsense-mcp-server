"""Unit tests for ``surfsense_mcp.auth.http``.

These cover the two helpers in isolation. End-to-end behavior (the dispatcher
in :mod:`surfsense_mcp.auth` actually injecting ``X-Auth-Request-User`` into
a SurfSense request) is covered by ``tests/test_http_mode_auth.py``.
"""

from __future__ import annotations

import time

import pytest
from fastmcp.server.auth.auth import AccessToken
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from surfsense_mcp.auth import http as http_auth


def _set_request_token(token: AccessToken):
    return auth_context_var.set(AuthenticatedUser(token))


def _reset_request_token(reset_token) -> None:
    auth_context_var.reset(reset_token)


def _make_token(claims: dict) -> AccessToken:
    return AccessToken(
        token="cognito-access-token-zzz",
        client_id="mcp-client",
        scopes=["openid"],
        expires_at=int(time.time() + 3600),
        claims=claims,
    )


def test_request_token_returns_none_outside_request_scope() -> None:
    """Stdio mode never has a FastMCP HTTP scope; ``get_access_token()``
    raises and the wrapper must swallow it to return ``None``."""
    assert http_auth.request_token() is None


def test_request_token_returns_access_token_inside_scope() -> None:
    token = _make_token({"sub": "abc-1234", "username": "alice"})
    reset = _set_request_token(token)
    try:
        assert http_auth.request_token() is token
    finally:
        _reset_request_token(reset)


def test_username_header_returns_x_auth_request_user_pair() -> None:
    token = _make_token({"sub": "abc-1234", "username": "alice"})
    assert http_auth.username_header(token) == {"X-Auth-Request-User": "alice"}


def test_username_header_raises_when_username_missing() -> None:
    """Missing claim is a hard error — silently sending no header would let
    the backend auto-provision under the wrong identity."""
    token = _make_token({"sub": "abc-1234"})
    with pytest.raises(RuntimeError, match="'username' claim missing"):
        http_auth.username_header(token)


def test_username_header_raises_when_username_empty_string() -> None:
    token = _make_token({"sub": "abc-1234", "username": ""})
    with pytest.raises(RuntimeError, match="'username' claim missing"):
        http_auth.username_header(token)


def test_username_header_raises_when_username_wrong_type() -> None:
    """A claim provider that hands us a non-string username (e.g. None or a
    list) should raise rather than coerce — the wrong type usually means a
    pool-config problem the operator needs to fix."""
    token = _make_token({"sub": "abc-1234", "username": ["alice"]})
    with pytest.raises(RuntimeError, match="'username' claim missing"):
        http_auth.username_header(token)


def test_username_header_raises_when_claims_dict_empty() -> None:
    token = _make_token({})
    with pytest.raises(RuntimeError, match="'username' claim missing"):
        http_auth.username_header(token)


def test_bearer_header_returns_authorization_bearer_pair() -> None:
    """``bearer_header`` forwards the raw JWT untouched — that is the same
    string oauth2-proxy will validate against the Cognito JWKS."""
    token = _make_token({"sub": "abc-1234", "username": "alice"})
    assert http_auth.bearer_header(token) == {
        "Authorization": "Bearer cognito-access-token-zzz"
    }


def test_bearer_header_raises_when_raw_token_empty() -> None:
    """An ``AccessToken`` with no raw JWT means we have nothing to forward to
    oauth2-proxy — fail loudly rather than send a malformed Authorization header."""
    from fastmcp.server.auth.auth import AccessToken

    token = AccessToken(
        token="",
        client_id="mcp-client",
        scopes=["openid"],
        expires_at=int(time.time() + 3600),
        claims={"username": "alice"},
    )
    with pytest.raises(RuntimeError, match="no raw token string"):
        http_auth.bearer_header(token)


def test_auth_headers_for_token_picks_bearer_for_https(monkeypatch) -> None:
    """HTTPS base URL → request will traverse Traefik + mPass; identity must
    ride ``Authorization`` because Traefik strips ``X-Auth-Request-*``."""
    monkeypatch.setenv("SURFSENSE_BASE_URL", "https://foss-research.local.moneta.dev")
    token = _make_token({"sub": "abc-1234", "username": "alice"})
    headers = http_auth.auth_headers_for_token(token)
    assert headers == {"Authorization": "Bearer cognito-access-token-zzz"}


def test_auth_headers_for_token_picks_username_for_http(monkeypatch) -> None:
    """HTTP base URL → direct docker-network call, no Traefik in the path; we
    inject the identity header ourselves and skip the (irrelevant) Bearer."""
    monkeypatch.setenv("SURFSENSE_BASE_URL", "http://surfsense-backend:8000")
    token = _make_token({"sub": "abc-1234", "username": "alice"})
    headers = http_auth.auth_headers_for_token(token)
    assert headers == {"X-Auth-Request-User": "alice"}


def test_auth_headers_for_token_defaults_to_username_when_unset(monkeypatch) -> None:
    """No URL configured at all → fall through to ``username_header``. The
    base-URL validator in ``client._base_url`` will fail the call later — at
    this layer we just want predictable behavior, not surprise bearers."""
    monkeypatch.delenv("SURFSENSE_BASE_URL", raising=False)
    token = _make_token({"sub": "abc-1234", "username": "alice"})
    headers = http_auth.auth_headers_for_token(token)
    assert headers == {"X-Auth-Request-User": "alice"}
